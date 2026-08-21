"""The agent loop.

One entry point — `respond()` — used by every channel: the web chat panel, the
Telegram bot, the scheduler. It builds the model's view of the world (system
prompt + recent conversation + the tools this person is allowed to use), lets the
model work, and enforces the autonomy policy on every tool call it asks for:

    auto → выполняем и рассказываем, что сделали
    ask  → готовим действие, кладём в pending_actions и ждём «да»
    off  → инструмента просто нет в списке, модель о нём не знает

Every invocation lands in `action_log`, which is what the «Что агент делал сегодня»
card shows. Nothing here is module-specific.
"""
import json
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.agent import honesty, policy, registry, tracing, voice
from app.agent.llm import (
    ROUTINE, LLMClient, LLMUnavailable, ToolCall, client as default_client, image_part, text_part,
)
from app.agent.prompts import system_prompt
from app.agent.registry import ToolContext, ToolResult, ToolSpec
from app.core import instructions, media
from app.core.config import settings
from app.core.events import ACTION_PENDING, bus
from app.core.logging import get_logger
from app.core.models import (
    MODE_ASK, MODE_AUTO, ActionLog, ChatMessage, PendingAction, User,
)

logger = get_logger("agent")

#: Сколько раз подряд агент может брать инструмент за один ответ. Ручка
#: `AGENT_MAX_STEPS`; умолчание живёт в `app/core/config.py`.
MAX_STEPS = settings.agent_max_steps
HISTORY_LIMIT = 16       # сообщений истории, которые видит модель
#: Сколько знаков сводки инструмента доезжает до следующего хода. Было 120 —
#: длинные сводки (recall с источниками, отказ поиска с причиной) резались на
#: полуслове, и модель на следующем ходу их переспрашивала.
TRAIL_SUMMARY_LIMIT = 200

OFFLINE_REPLY = (
    "Сейчас не могу подумать — не отвечает модель. "
    "Записи и уведомления при этом работают: попробуйте ещё раз чуть позже."
)

#: Чем открывается служебная запись о работе инструментов. Слова важны дважды:
#: по ним модель понимает, что запись не её, и по ним же мы узнаём собственную
#: подделку в её ответе (`_looks_fabricated`).
TRAIL_HEADER = "Что вернули инструменты (служебная запись системы, не твои слова):"

#: Что сказать модели, поймав её на выдуманном перечне вызовов (детекторы —
#: в app/agent/honesty.py). Формулировка резкая по той же
#: причине, что и у «СТОП» в `_handle_call`: мягкую модель дочитывает как совет.
FABRICATION_NUDGE = (
    "СТОП: ты перечислил вызовы инструментов, но не вызвал ни одного — значит, "
    "ничего не сделано, и говорить человеку обратное нельзя. Такие перечни пишет "
    "система, а не ты. Нужно действие — вызови инструмент прямо сейчас; не нужно — "
    "ответь одними словами, без перечня."
)

#: Отчёт о сделанном без единого вызова — тот же обман, только без перечня.
#: Прогон #81: «Готово, я записала эти две идеи на доску», ноль вызовов, на доске
#: пусто. Перечня, за который цеплялся `_looks_fabricated`, в таком ответе нет —
#: есть одно слово «готово», и снаружи оно неотличимо от работы.
CLAIM_NUDGE = (
    "СТОП: ты сказал человеку, что уже сделал, но не вызвал ни одного инструмента "
    "— значит, не сделано ничего, и это неправда. Нужно действие — вызови "
    "инструмент прямо сейчас; не нужно — ответь словами, но без «готово», "
    "«записал», «запомнил» и прочего о несделанном."
)

#: Чем заменяется ответ, в котором модель второй раз подряд отчитывается о том,
#: чего не делала. Своих слов у нас тут нет: работы не было, и сказать о ней
#: нечего, кроме правды.
UNBACKED_REPLY = (
    "Похоже, у меня не вышло это сделать — ничего не записано. "
    "Давайте попробуем ещё раз?"
)

#: Каноническая строка для упора в лимит шагов. Осталась запасной: когда
#: инструменты успели что-то сделать или модель может сказать то же характером,
#: человеку уходит честный пересказ, а не эта заготовка (`_overflow_answer`).
BURIED_REPLY = "Похоже, я закопался в подсчётах. Давайте попробуем сформулировать проще?"

#: Просьбы к голосу персоны (`app/agent/voice.py`) на аварийных путях. Все они
#: пересказывают уже случившееся и прямо запрещают выдумывать: факт наружу,
#: манера — от характера.
_UNBACKED_HINT = (
    "У тебя не получилось сделать то, о чём просил человек: ни один инструмент "
    "не отработал, ничего не записано. Скажи ему это честно одной-двумя фразами "
    "в своей манере и предложи попробовать ещё раз. Не говори «готово» и "
    "«записал» и ничего не выдумывай."
)
_BURIED_HINT = (
    "Ты запутался и не довёл ответ до конца: ничего не сделано. Скажи это честно "
    "одной фразой в своей манере и попроси человека сформулировать проще. "
    "Не говори «готово» и ничего не выдумывай."
)
_OVERFLOW_DONE_HINT = (
    "Ты упёрся в лимит шагов и не довёл дело до конца, но часть работы сделана. "
    "Перескажи человеку одной-двумя фразами в своей манере, что успел, — строго "
    "по фактам ниже, ничего не добавляя, — и предложи продолжить, если нужно:\n{facts}"
)
_SILENT_AWAITING_HINT = (
    "Ты подготовил действие, и оно ждёт подтверждения человека. Скажи одной "
    "фразой в своей манере, что подготовил и ждёшь его «да». "
    "Не говори, что уже сделал."
)
_SILENT_DONE_HINT = (
    "Инструменты отработали, а слов для человека у тебя не нашлось. Перескажи "
    "ему одной-двумя фразами в своей манере, что сделано, — строго по фактам, "
    "ничего не добавляя:\n{facts}"
)
_SILENT_EMPTY_HINT = (
    "Ответ не сложился: ничего не сделано и сказать нечего. Попроси человека "
    "одной фразой в своей манере сказать ещё раз. Не говори «готово» и ничего "
    "не выдумывай."
)

#: Чем помечается в истории реплика с таким отчётом. Модель, читающая свои
#: прошлые «готово» как правду, повторяет узор ходом позже (прогон #82) и заодно
#: ссылается на несделанное как на сделанное.
NOTHING_HAPPENED = (
    "Реплика ассистента выше не подкреплена ни одним вызовом инструмента: ничего "
    "из обещанного в ней не сделано. Не считай это правдой и не ссылайся на это "
    "как на сделанное."
)

#: Детекторы отчётов о несделанном вынесены в app/agent/honesty.py вместе с
#: опциональным LLM-судьёй второй ступени (HONESTY_JUDGE=1). Здесь остаются
#: тонкие обёртки: имена прижились и в этом файле, и в его тестах.
_own_words = honesty.own_words
_looks_fabricated = honesty.looks_fabricated
_claims_done = honesty.claims_done


@dataclass
class Trace:
    """The «плашка инструмента» over the agent's reply."""
    tool: str
    title: str
    arguments: Dict[str, Any]
    status: str                       # done | awaiting | failed | confirmed
    summary: str = ""
    pending_id: Optional[int] = None
    #: Что инструмент вернул машинно — прежде всего номера записей. В словах
    #: сводки их нет («Записал: пицца — 1050 ккал»), а следующему ходу они нужны,
    #: чтобы поправка знала, о какой именно записи речь.
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        args = ", ".join(f"{k}={_short(v)}" for k, v in self.arguments.items())
        return f"{self.tool}({args})" if args else f"{self.tool}()"


@dataclass
class AgentReply:
    text: str
    traces: List[Trace] = field(default_factory=list)
    cards: List[Dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "traces": [dict(asdict(t), signature=t.signature) for t in self.traces],
            "cards": self.cards,
        }


def _short(value: Any, limit: int = 24) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- conversation history -------------------------------------------------

def _trail(payload: dict) -> str:
    """Чем ассистент занимался на прошлом ходу — строкой для следующего хода.

    Вызовы инструментов живут в `messages` внутри одного ответа и умирают вместе
    с ним: наружу уезжает только текст реплики. Из-за этого ассистент не помнит
    ни номера записи, которую сам же завёл, ни того, что поиск уже отвечал
    отказом, — и на «пицца была 20 см» ему остаётся выдумывать цифры.

    Восстанавливается это не настоящими `tool_calls`, а служебной записью.
    Причина в окне: пара «assistant с tool_calls» + «tool с ответом» неразрывна,
    и скользящее окно рано или поздно разрежет её пополам — половина провайдеров
    на такую историю отвечает ошибкой. Записи резать нечего.

    Едет она отдельным сообщением, а не припиской к реплике (`load_history`).
    """
    lines = []
    for trace in payload.get("traces") or []:
        if not isinstance(trace, dict):
            continue
        name = trace.get("signature") or trace.get("tool") or "?"
        outcome = _short(trace.get("summary") or "", TRAIL_SUMMARY_LIMIT)
        data = {k: v for k, v in (trace.get("data") or {}).items() if v is not None}
        line = f"{name} → {trace.get('status') or '?'}"
        if outcome:
            line += f": {outcome}"
        if data:
            line += " [" + ", ".join(f"{k}={v}" for k, v in data.items()) + "]"
        lines.append(line)
    return "\n".join(lines)


def load_history(db: Session, user: User, limit: int = HISTORY_LIMIT) -> List[dict]:
    """История разговора глазами модели: реплики и служебные записи между ними.

    След инструментов идёт отдельным сообщением, а не припиской к реплике
    ассистента, и это не оформление. Приписка внутри реплики показывала модели
    ход за ходом, что реплика ассистента выглядит вот так: слова, а под ними
    перечень вызовов. Языковая модель продолжает узор, который видит, — и в
    какой-то момент дописывает перечень сама, не вызвав ничего. Прогон #74:
    «переименовала блюдо и запомнила рецепт», ноль вызовов, оба действия не
    сделаны. Своих слов в чужой роли модель не пишет, поэтому узор и переехал
    из её реплики наружу.
    """
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    history = []
    for row in reversed(rows):
        # В базе могли осесть реплики с выдуманным (и со старым, настоящим)
        # перечнем вызовов — в модель они не едут ни в каком виде.
        content = _own_words(row.content)
        if content:
            history.append({"role": row.role, "content": content})
        if row.role != "assistant":
            continue
        trail = _trail(message_payload(row))
        if trail:
            # Служебная запись живёт только в запросе к модели: в базе строка
            # остаётся чистой, а панель читает её же — человеку это ни к чему.
            history.append({"role": "system", "content": f"{TRAIL_HEADER}\n{trail}"})
        elif content and _claims_done(content):
            # «Готово, записала» без единого вызова. Такие строки осели в базе
            # до этой починки, и молчать о них нельзя дважды: модель читает их
            # как узор своей роли (сказал «готово» — и ход закрыт) и как правду
            # о состоянии данных («я же это уже записала»).
            history.append({"role": "system", "content": NOTHING_HAPPENED})
    return history


def _unbacked(text: str, traces: List[Trace]) -> bool:
    """Ответ рассказывает о работе, которой не было.

    Два вида одного обмана: выдуманный перечень вызовов и просто «готово».
    Второй ловится только на пустом следе — когда инструмент на этом ходу
    отработал, «записал» правда, а разбираться, обо всём ли из сказанного
    отчитались честно, слова не позволяют.
    """
    if _looks_fabricated(text):
        return True
    return not traces and _claims_done(_own_words(text))


def save_message(db: Session, user: User, role: str, content: str,
                 channel: str = "web", payload: dict = None) -> ChatMessage:
    message = ChatMessage(
        user_id=user.id,
        role=role,
        content=content,
        channel=channel,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def message_payload(message: ChatMessage) -> dict:
    if not message.payload_json:
        return {"traces": [], "cards": []}
    try:
        return json.loads(message.payload_json)
    except ValueError:
        return {"traces": [], "cards": []}


# --- the loop -------------------------------------------------------------

class Agent:
    def __init__(self, llm: LLMClient = None):
        self.llm = llm or default_client

    def respond(
        self,
        db: Session,
        actor: User,
        text: str,
        image: bytes = None,
        image_mime: str = "image/jpeg",
        channel: str = "web",
        subject: User = None,
    ) -> AgentReply:
        """Answer one message from a human, running tools as policy allows."""
        subject = subject or actor
        with tracing.run(db, actor, subject, channel, text or "(фото)"):
            return self._respond(db, actor, subject, text, image, image_mime, channel)

    def _respond(self, db: Session, actor: User, subject: User, text: str,
                 image: bytes, image_mime: str, channel: str) -> AgentReply:
        attachments = {"image": image, "image_mime": image_mime} if image else {}
        ctx = ToolContext(db=db, actor=actor, subject=subject, channel=channel, attachments=attachments)

        specs = {s.name: s for s in policy.available_tools(db, subject)}
        modules = sorted({s.module for s in specs.values()})

        history = load_history(db, actor)
        save_message(db, actor, "user", text or "(фото)", channel=channel)

        user_content: Any = text or "Оцени, пожалуйста, что на фото."
        if image:
            user_content = [text_part(user_content), image_part(image, image_mime)]

        # Характер и памятки — того, за кого работает ассистент: весь системный
        # промпт написан про него, от имени до нормы самостоятельности. Памятки
        # едут только по включённым модулям — по тем же, что дали инструменты.
        #
        # Правила — исключение: они записи на доске, а доски смотрят глазами
        # того, кто разговаривает, а не «от лица» (ADR-0005). Едут параметром,
        # а не припиской в хвост, как перечень досок: место у них в промпте
        # определённое — рядом с характером и выше того, что не отменяется ничем.
        rules = ()
        if "memory" in modules:
            from app.modules.memory.knowledge import rules_for_prompt
            rules = rules_for_prompt(db, actor.id)
        # Ручки — того же человека, что и характер. В промпт едет не только
        # уровень, но и чей он, и личные исключения по инструментам: человек
        # правит всё это словами, и ассистент, не знающий текущего положения,
        # менял бы его вслепую (ADR-0012).
        resolved = policy.dials(db, subject)
        system = system_prompt(
            subject, modules,
            character=instructions.character(subject),
            memos=instructions.for_prompt(db, subject.id, modules),
            autonomy=resolved.autonomy,
            own_autonomy=not resolved.follows_family,
            tool_exceptions=[(row["spec"].title, row["spec"].name, row["mode"])
                             for row in policy.own_exceptions(db, subject)],
            rules=rules,
        )
        if "memory" in modules:
            # Названия и инструкции досок — в промпт; содержимое остаётся за
            # read_board, автообогащения нет (спека #19). Доски — глазами того,
            # кто разговаривает, а не «от лица» (ADR-0005), как и сами инструменты.
            from app.modules.memory.knowledge import boards_prompt
            boards = boards_prompt(db, actor.id)
            if boards:
                system += f"\n{boards}"
        if "relationship" in modules:
            # Память за пределами окна истории: последние итоги разговоров,
            # которые пишет разбор «Подхода». Ноль новых LLM-вызовов — итоги
            # уже лежат на доске (тикет #77).
            from app.agent.prompts import summaries_block
            from app.modules.relationship.service import recent_summaries
            summaries = summaries_block(recent_summaries(db, actor.id))
            if summaries:
                system += f"\n{summaries}"
        messages: List[dict] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        traces: List[Trace] = []
        cards: List[Dict[str, Any]] = []
        answer = ""

        nudged = False
        #: Реплика, которую судья честности уже оправдал в этом ходу, — финальная
        #: проверка не судит её второй раз.
        judged_clear = None
        try:
            for _ in range(MAX_STEPS):
                response = self.llm.chat(
                    messages,
                    tools=[registry.openai_schema(s) for s in specs.values()] or None,
                    # Работа этого вызова — выбрать инструмент и ответить парой фраз.
                    # Думать над ней нечего; думают, если надо, сами инструменты
                    # (ADR-0007).
                    task=ROUTINE,
                )
                if not response.tool_calls:
                    answer = _own_words(response.content)
                    # Модель отчиталась о работе вместо работы. Молча срезать
                    # перечень мало: слова над ним («переименовала, запомнила»)
                    # останутся такой же неправдой, а «готово» без перечня и
                    # срезать нечего. Показываем ей, что вышло, и даём сделать
                    # по-настоящему — один раз, чтобы не кружить.
                    # Выдуманный перечень — улика структурная, судья не нужен;
                    # голое «готово» с включённой второй ступенью переспрашивается
                    # (`honesty.confirmed_claim`), чтобы не глушить живую фразу
                    # зря. Оправданная судьёй реплика запоминается — финальная
                    # проверка не судит её второй раз.
                    if not nudged and _unbacked(response.content, traces):
                        if (_looks_fabricated(response.content)
                                or honesty.confirmed_claim(_own_words(response.content), self.llm)):
                            nudged = True
                            logger.warning("Модель отчиталась о работе, которой не было "
                                           "— прошу вызвать инструменты по-настоящему")
                            messages.append({"role": "assistant", "content": response.content})
                            messages.append({"role": "system", "content": (
                                FABRICATION_NUDGE if _looks_fabricated(response.content)
                                else CLAIM_NUDGE)})
                            continue
                        judged_clear = answer
                    break

                messages.append({
                    "role": "assistant",
                    "content": _own_words(response.content) or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                        for c in response.tool_calls
                    ],
                })

                for call in response.tool_calls:
                    trace, tool_message, card = self._handle_call(db, ctx, specs, call)
                    traces.append(trace)
                    if card:
                        cards.append(card)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": tool_message})
            else:
                answer = _overflow_answer(subject, traces, self.llm)
        except LLMUnavailable:
            # Не сохраняем в chat_messages: иначе следующий вызов увидит этот текст как
            # свою же прошлую реплику и при повторном сбое начнёт его зацикленно повторять
            # (это ровно то, что случилось в run 180 — восемь одинаковых ответов подряд).
            reply = AgentReply(text=OFFLINE_REPLY)
            tracing.finish(reply.text)
            return reply

        if not answer:
            answer = _voiced_silence(subject, traces, self.llm)

        # Модель настояла на своём: второй раз подряд «готово» без единого вызова.
        # Слова над пустотой человеку отдавать нельзя — по ним он уйдёт уверенным,
        # что запись есть. Разбирать, что в ответе правда, а что нет, мы не умеем,
        # поэтому меняем его целиком: потерянная живая фраза — цена, ложное
        # «записала» — нет. Правду при этом можно сказать характером — но только
        # правду: реплика голоса, снова похожая на отчёт, глушится канонической
        # строкой тем же детектором.
        if (not traces and _claims_done(answer) and answer != judged_clear
                and honesty.confirmed_claim(answer, self.llm)):
            logger.warning("Модель отчиталась о работе, которой не было, — "
                           "отвечаю человеку правду вместо её слов")
            voiced = voice.speak(subject, _UNBACKED_HINT, fallback=UNBACKED_REPLY, llm=self.llm)
            answer = UNBACKED_REPLY if _claims_done(voiced) else voiced

        reply = AgentReply(text=answer, traces=traces, cards=cards)
        save_message(db, actor, "assistant", answer, channel=channel, payload=reply.to_payload())
        tracing.finish(answer)
        return reply

    def _handle_call(self, db: Session, ctx: ToolContext, specs: Dict[str, ToolSpec], call: ToolCall):
        spec = specs.get(call.name)
        if spec is None:
            logger.warning(f"Модель попросила неизвестный или недоступный инструмент: {call.name}")
            _trace_tool(call.name, call.arguments, {"отказ": "инструмент недоступен"}, status="failed")
            trace = Trace(tool=call.name or "?", title=call.name or "?", arguments=call.arguments,
                          status="failed", summary="Инструмент недоступен")
            return trace, "Такого инструмента нет или он выключен для этого человека.", None

        mode = policy.resolve_mode(db, ctx.subject, spec)

        if mode == MODE_ASK:
            image = ctx.attachments.get("image")
            pending = PendingAction(
                user_id=ctx.subject.id,
                tool=spec.name,
                arguments_json=json.dumps(call.arguments, ensure_ascii=False),
                # фото не переживёт JSON-аргументы — паркуем его рядом с действием
                attachment_path=media.stage_attachment(image, ctx.subject.id) if image else None,
            )
            db.add(pending)
            db.commit()
            db.refresh(pending)
            bus.publish(ACTION_PENDING, {"pending_id": pending.id, "user_id": ctx.subject.id,
                                         "tool": spec.name, "channel": ctx.channel})
            _trace_tool(spec.name, call.arguments,
                        {"режим": MODE_ASK, "pending_id": pending.id}, status="awaiting")
            trace = Trace(tool=spec.name, title=spec.title, arguments=call.arguments,
                          status="awaiting", summary="ждёт подтверждения", pending_id=pending.id)
            card = {"type": "confirm", "pending_id": pending.id, "tool": spec.name,
                    "title": spec.title, "arguments": call.arguments}
            # Формулировка нарочно резкая: модель охотно пишет «удалил» о том, что
            # ещё не сделано, и человек уходит уверенным, что записи больше нет.
            return trace, (
                f"СТОП: «{spec.title}» ещё НЕ выполнено. Действие подготовлено и ждёт «да» "
                f"от человека. Скажи, что именно собираешься сделать, и попроси подтверждения. "
                f"Не пиши «сделал», «удалил», «записал», «готово» — это будет неправдой. "
                f"Повторно инструмент не вызывай."
            ), card

        started = time.monotonic()
        result = registry.execute(spec, ctx, call.arguments)
        _trace_tool(spec.name, call.arguments,
                    {"ok": result.ok, "summary": result.summary, "card": result.card},
                    status="ok" if result.ok else "failed",
                    duration_ms=int((time.monotonic() - started) * 1000))
        _log_action(db, ctx.subject, spec, call.arguments, result, mode=MODE_AUTO)
        trace = Trace(tool=spec.name, title=spec.title, arguments=call.arguments,
                      status="done" if result.ok else "failed", summary=result.summary,
                      data=result.data or {})
        return trace, result.summary, result.card


def _trace_tool(name: str, arguments: Any, result: Any, status: str, duration_ms: int = 0):
    """Вызов инструмента — в трейс прогона, если запись включена."""
    recorder = tracing.current()
    if recorder is not None:
        recorder.tool(name or "?", arguments, result, status=status, duration_ms=duration_ms)


def _fallback_answer(traces: List[Trace]) -> str:
    """The model ran a tool but said nothing — say something honest ourselves."""
    if any(t.status == "awaiting" for t in traces):
        return "Подготовил действие — подтвердите, и я его выполню."
    done = [t for t in traces if t.status == "done"]
    if done:
        return done[-1].summary
    # Ни слов, ни работы: «Готово» здесь было бы отчётом ни о чём.
    return "Похоже, ответ не сложился. Скажите, пожалуйста, ещё раз?"


def _facts(traces: List[Trace]) -> str:
    return "\n".join(f"- {t.summary}" for t in traces if t.status == "done" and t.summary)


def _voiced_silence(subject: User, traces: List[Trace], llm: LLMClient) -> str:
    """Модель промолчала — сказать честное самим, но голосом персоны.

    Факты и запасная строка — прежние (`_fallback_answer`); голос лишь
    пересказывает их характером. В ветках без сделанной работы реплика голоса,
    похожая на отчёт о сделанном, глушится запасной строкой: у аварийного пути
    не может быть права соврать красивее.
    """
    fallback = _fallback_answer(traces)
    done = [t for t in traces if t.status == "done"]
    if any(t.status == "awaiting" for t in traces):
        hint = _SILENT_AWAITING_HINT
    elif done:
        hint = _SILENT_DONE_HINT.format(facts=_facts(traces))
    else:
        hint = _SILENT_EMPTY_HINT
    voiced = voice.speak(subject, hint, fallback=fallback, llm=llm)
    if not done and _claims_done(voiced):
        return fallback
    return voiced


def _overflow_answer(subject: User, traces: List[Trace], llm: LLMClient) -> str:
    """Лимит шагов исчерпан. Раньше это всегда было «закопался в подсчётах» —
    даже когда инструменты успели отработать и человеку было что рассказать.
    Теперь: успел что-то сделать — честный пересказ сделанного, не успел ничего
    — то же «закопался», по возможности голосом персоны."""
    done = [t for t in traces if t.status == "done"]
    if done:
        return voice.speak(subject, _OVERFLOW_DONE_HINT.format(facts=_facts(traces)),
                           fallback=_fallback_answer(traces), llm=llm)
    voiced = voice.speak(subject, _BURIED_HINT, fallback=BURIED_REPLY, llm=llm)
    return BURIED_REPLY if _claims_done(voiced) else voiced


def _log_action(db: Session, user: User, spec: ToolSpec, arguments: dict,
                result: ToolResult, mode: str, channel: str = None):
    db.add(ActionLog(
        user_id=user.id,
        tool=spec.name,
        arguments_json=json.dumps(arguments, ensure_ascii=False),
        outcome="done" if result.ok else "failed",
        mode=channel or mode,
        summary=result.summary[:500],
    ))
    db.commit()


# --- confirmations --------------------------------------------------------

#: `approve_action`/`reject_action` живут вне обычного хода `_respond`, и там
#: некому пересказать факт голосом ассистента — это делает следующий ход
#: модели внутри цикла (см. `_handle_call`: summary инструмента едет ей
#: сообщением `role: tool`). Здесь тот же смысл, но отдельным, коротким
#: вызовом без инструментов и истории: без него человеку доставался сырой
#: `ToolResult.summary` — техническая строка без характера, и заведённая
#: девушке-ассистенту личность на этом шаге пропадала (#78).
_APPROVE_HINT = (
    "Человек только что нажал «да, сделай» на подготовленное действие. Скажи "
    "ему одной-двумя фразами в своей манере, что сделано, — строго по этому "
    "факту, ничего не добавляя и не выдумывая:\n{fact}"
)
_REJECT_HINT = (
    "Человек отказался от подготовленного действия — нажал «не надо». "
    "Подтверди отказ одной короткой фразой в своей манере: ты ничего не делаешь."
)


def approve_action(db: Session, pending_id: int, actor: User, channel: str = "web",
                   llm: LLMClient = None) -> ToolResult:
    """Run a prepared action after the human said yes."""
    pending = db.get(PendingAction, pending_id)
    if pending is None or pending.status != "pending":
        return ToolResult(summary="Это действие уже неактуально.", ok=False)

    subject = db.get(User, pending.user_id)
    # Подтверждает только тот, чей это разговор: чужое «да» больше не выдаётся
    # никому — роль главы семьи, умевшая это, разделилась на админа и участника,
    # и у администратора разговора нет вовсе (ADR-0008).
    if subject is None or actor.id != subject.id:
        return ToolResult(summary="Подтвердить это действие может только сам человек.", ok=False)

    spec = registry.get(pending.tool)
    if spec is None:
        pending.status = "expired"
        db.commit()
        return ToolResult(summary="Инструмент больше не доступен.", ok=False)

    arguments = json.loads(pending.arguments_json or "{}")
    image = media.read_and_discard(pending.attachment_path)
    # Действие исполняется в контексте того, чей это был разговор: инструменты
    # знаний ходят по ctx.actor (ADR-0005).
    ctx = ToolContext(db=db, actor=subject, subject=subject, channel=channel,
                      attachments={"image": image} if image else {})
    result = registry.execute(spec, ctx, arguments)

    pending.status = "approved"
    pending.resolved_at = datetime.utcnow()
    # В базе остаётся то, что вернул инструмент, — сырым: это техническая
    # запись для лога и панели, не реплика в разговоре.
    pending.result_summary = result.summary[:500]
    db.commit()

    _log_action(db, subject, spec, arguments, result, mode="confirmed")
    spoken = voice.speak(subject, _APPROVE_HINT.format(fact=result.summary),
                         fallback=result.summary, llm=llm or default_client)
    reply = replace(result, summary=spoken)
    save_message(db, actor, "assistant", spoken, channel=channel,
                 payload=AgentReply(text=spoken,
                                    traces=[Trace(tool=spec.name, title=spec.title, arguments=arguments,
                                                  status="confirmed", summary=spoken,
                                                  data=result.data or {})],
                                    cards=[result.card] if result.card else []).to_payload())
    return reply


def reject_action(db: Session, pending_id: int, actor: User, channel: str = "web",
                  llm: LLMClient = None) -> ToolResult:
    pending = db.get(PendingAction, pending_id)
    if pending is None or pending.status != "pending":
        return ToolResult(summary="Это действие уже неактуально.", ok=False)
    if actor.id != pending.user_id:
        return ToolResult(summary="Отменить это действие может только сам человек.", ok=False)

    spec = registry.get(pending.tool)
    media.read_and_discard(pending.attachment_path)   # вложение больше не нужно
    pending.status = "rejected"
    pending.resolved_at = datetime.utcnow()
    db.commit()

    spoken = voice.speak(actor, _REJECT_HINT, fallback="Хорошо, не делаю.",
                         llm=llm or default_client)
    # Сохраняем репликой в разговоре — иначе после перезагрузки экрана отказ
    # пропадает бесследно, а модель на следующем ходу не знает, что просьбу
    # сняли, и продолжает читать её как «ждёт подтверждения».
    save_message(db, actor, "assistant", spoken, channel=channel,
                 payload=AgentReply(text=spoken,
                                    traces=[Trace(tool=pending.tool,
                                                  title=spec.title if spec else pending.tool,
                                                  arguments=json.loads(pending.arguments_json or "{}"),
                                                  status="rejected", summary=spoken)]).to_payload())
    return ToolResult(summary=spoken)


def run_tool_directly(db: Session, subject: User, tool_name: str, arguments: dict,
                      mode: str = "event", actor: User = None) -> ToolResult:
    """Invoke a tool outside the chat — from an event handler or a schedule.

    Bypasses the LLM but not the log: scheduled and event-driven work shows up in
    «Что агент делал сегодня» the same way chat-driven work does.
    """
    spec = registry.get(tool_name)
    if spec is None:
        return ToolResult(summary=f"Неизвестный инструмент: {tool_name}", ok=False)
    ctx = ToolContext(db=db, actor=actor or subject, subject=subject, channel=mode)
    result = registry.execute(spec, ctx, arguments)
    _log_action(db, subject, spec, arguments, result, mode=mode)
    return result


agent = Agent()
