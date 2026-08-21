"""Движок идей — ассистент раз в неделю сам предлагает, что упростить.

«Вы каждый вечер записываете шаги руками — поставить табло?» — такие вещи видно
только сверху: по логу действий, памяткам и итогам разговоров. Раз в неделю
один вызов модели на человека собирает из этого 0–2 предложения на доску
«Идеи ассистента» и строку в еженедельную сводку.

Три жёстких правила:
  * ассистент только предлагает — движок ничего не делает сам и не зовёт ни
    одного инструмента, что бы модель ни вернула;
  * не больше IDEAS_PER_WEEK предложений в неделю: идеи, приходящие пачками,
    перестают читаться;
  * опт-аут — правилом со словами про идеи («не предлагай мне идей») или
    выключенным модулем «Подход»: движок — часть той же подстройки под
    человека, что и разбор разговоров.
"""
from datetime import datetime, timedelta
from typing import List

from sqlalchemy.orm import Session

from app.agent.llm import ROUTINE, LLMUnavailable, client as default_client
from app.core.logging import get_logger
from app.core.models import ActionLog, User
from app.modules.memory import knowledge

logger = get_logger("ideas")

#: Не больше стольких предложений за один недельный прогон.
IDEAS_PER_WEEK = 2
#: Ёмкость доски — старые предложения ассистента вытесняются, как заметки о подходе.
IDEAS_MAX = 20
#: Раз в сколько дней движок смотрит на человека.
EVERY_DAYS = 7
#: Меньше стольких действий за неделю — смотреть не на что, человек и так
#: почти не пользуется ассистентом, и заваливать его идеями невежливо.
MIN_ACTIONS = 5

IDEAS_SYSTEM = """\
Ты смотришь на то, как один человек пользуется семейным ассистентом, и
предлагаешь, что можно упростить или завести. Отвечай ТОЛЬКО объектом JSON:

{"ideas": ["одно предложение, конкретное и короткое", "..."]}

Правила:
- Не больше двух идей; чаще всего — одна или ни одной. Пустой список — обычный
  честный ответ: нет идеи — не выдумывай.
- Идея — конкретное предложение из жизни этого человека: «вы каждый вечер
  записываете шаги руками — поставить табло?», «напоминание про лекарство
  ставится каждый день — сделать его повторяющимся?», «у доски „Кормления“
  нет инструкции — сформулировать?»
- Только предложение, никакого действия: решает человек, словами в разговоре.
- Не повторяй идеи, которые уже лежат на доске, — их список дан.
- Никаких нравоучений, оценок здоровья и советов «вести здоровый образ жизни».
"""


def due(db: Session, user_id: int, now: datetime = None) -> bool:
    """Пора ли смотреть: доска не трогалась EVERY_DAYS. Прогон без единой идеи
    тоже «трогает» доску (см. run_ideas) — иначе он повторялся бы каждый тик."""
    now = now or datetime.utcnow()
    board = knowledge.ideas_board(db, user_id, create=False)
    if board is None:
        return True
    return board.last_activity_at <= now - timedelta(days=EVERY_DAYS)


def muted(db: Session, user_id: int) -> bool:
    """Опт-аут правилом: человек однажды сказал «не предлагай мне идей».

    Разбор грубый — по словам, как и остальные наши эвристики: правило с «иде»
    и с «не предлагай»/«без» читается как отказ. Ошибка в сторону молчания
    дешевле ошибки в сторону навязчивости.
    """
    for rule in knowledge.list_rules(db, user_id):
        text = rule.text.lower()
        if "иде" in text and ("не предлагай" in text or "без " in text):
            return True
    return False


def _week_actions(db: Session, user_id: int, now: datetime) -> List[str]:
    """Сводка action_log за неделю: «log_activity — 6 раз», частые первыми."""
    since = now - timedelta(days=7)
    counts: dict = {}
    for row in (db.query(ActionLog)
                .filter(ActionLog.user_id == user_id, ActionLog.created_at >= since)):
        counts[row.tool] = counts.get(row.tool, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: -pair[1])
    return [f"{tool} — {n} раз" for tool, n in ordered]


def _material(db: Session, user: User, now: datetime) -> str:
    from app.core import instructions
    from app.modules.relationship.service import recent_summaries

    actions = _week_actions(db, user.id, now)
    memos = instructions.memos(db, user.id)
    boards = [f"«{g.board.name}»" + (" (без инструкции)" if not g.board.instruction else "")
              for g in knowledge.board_grants(db, user.id)]
    summaries = recent_summaries(db, user.id)
    board = knowledge.ideas_board(db, user.id, create=False)
    existing = ([e.text for e in knowledge.list_entries(db, user.id, board.id)]
                if board is not None else [])

    parts = [
        "Чем человек пользовался за неделю (инструмент — сколько раз):",
        "\n".join(f"- {line}" for line in actions) or "- ничем",
        "\nЕго доски:",
        "\n".join(f"- {line}" for line in boards) or "- нет",
    ]
    if memos:
        parts += ["\nЕго памятки:",
                  "\n".join(f"- {module}: {text}" for module, text in memos.items())]
    if summaries:
        parts += ["\nИтоги последних разговоров:",
                  "\n".join(f"- {line}" for line in summaries)]
    if existing:
        parts += ["\nИдеи, уже лежащие на доске (не повторяй):",
                  "\n".join(f"- {line}" for line in existing)]
    return "\n".join(parts)


def run_ideas(db: Session, user: User, llm=None, now: datetime = None) -> List[str]:
    """Один недельный прогон для одного человека. Возвращает записанные идеи."""
    now = now or datetime.utcnow()
    if muted(db, user.id):
        return []
    if len(_week_actions(db, user.id, now)) == 0 or (
            db.query(ActionLog)
            .filter(ActionLog.user_id == user.id,
                    ActionLog.created_at >= now - timedelta(days=7))
            .count() < MIN_ACTIONS):
        return []

    llm = llm or default_client
    try:
        raw = llm.json_completion(IDEAS_SYSTEM, _material(db, user, now), task=ROUTINE)
    except LLMUnavailable:
        logger.warning(f"Идеи для {user.display_name} отложены — модель недоступна")
        return []
    ideas = [str(text).strip() for text in (raw.get("ideas") or [])
             if str(text or "").strip()][:IDEAS_PER_WEEK] if isinstance(raw, dict) else []

    board = knowledge.ideas_board(db, user.id, create=True)
    for text in ideas:
        knowledge.add_assistant_entry(db, user.id, board.id, text)
    _enforce_cap(db, user.id, board.id)
    if not ideas:
        # Прогон был — трогаем доску, чтобы `due` не звал модель каждый тик.
        board.last_activity_at = now
        db.commit()
    return ideas


def _enforce_cap(db: Session, user_id: int, board_id: int) -> None:
    entries = knowledge.list_entries(db, user_id, board_id)
    overflow = len(entries) - IDEAS_MAX
    if overflow <= 0:
        return
    for entry in [e for e in entries if e.by_assistant][:overflow]:
        knowledge.delete_assistant_entry(db, user_id, entry.id)


def fresh_ideas(db: Session, user_id: int, days: int = EVERY_DAYS) -> List[str]:
    """Свежие предложения — для строки в еженедельной сводке."""
    board = knowledge.ideas_board(db, user_id, create=False)
    if board is None:
        return []
    cutoff = datetime.utcnow() - timedelta(days=days)
    return [e.text for e in knowledge.list_entries(db, user_id, board.id)
            if e.by_assistant and e.created_at >= cutoff]
