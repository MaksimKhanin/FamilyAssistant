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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.agent import policy, registry, tracing
from app.agent.llm import LLMClient, LLMUnavailable, ToolCall, client as default_client, image_part, text_part
from app.agent.prompts import system_prompt
from app.agent.registry import ToolContext, ToolResult, ToolSpec
from app.core import media
from app.core.events import ACTION_PENDING, bus
from app.core.logging import get_logger
from app.core.models import (
    MODE_ASK, MODE_AUTO, ActionLog, ChatMessage, PendingAction, User,
)

logger = get_logger("agent")

MAX_STEPS = 4            # сколько раз подряд агент может брать инструмент за один ответ
HISTORY_LIMIT = 16       # сообщений истории, которые видит модель

OFFLINE_REPLY = (
    "Сейчас не могу подумать — не отвечает модель. "
    "Записи и уведомления при этом работают: попробуйте ещё раз чуть позже."
)


@dataclass
class Trace:
    """The «плашка инструмента» over the agent's reply."""
    tool: str
    title: str
    arguments: Dict[str, Any]
    status: str                       # done | awaiting | failed | confirmed
    summary: str = ""
    pending_id: Optional[int] = None

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

def load_history(db: Session, user: User, limit: int = HISTORY_LIMIT) -> List[dict]:
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    return [{"role": row.role, "content": row.content} for row in reversed(rows) if row.content]


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

        system = system_prompt(subject, modules)
        if "memory" in modules:
            # Названия и инструкции досок — в промпт; содержимое остаётся за
            # read_board, автообогащения нет (спека #19). Доски — глазами того,
            # кто разговаривает, а не «от лица» (ADR-0005), как и сами инструменты.
            from app.modules.memory.knowledge import boards_prompt
            boards = boards_prompt(db, actor.id)
            if boards:
                system += f"\n{boards}"
        messages: List[dict] = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})

        traces: List[Trace] = []
        cards: List[Dict[str, Any]] = []
        answer = ""

        try:
            for _ in range(MAX_STEPS):
                response = self.llm.chat(
                    messages,
                    tools=[registry.openai_schema(s) for s in specs.values()] or None,
                )
                if not response.tool_calls:
                    answer = response.content
                    break

                messages.append({
                    "role": "assistant",
                    "content": response.content or None,
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
                answer = "Похоже, я закопался в подсчётах. Давайте попробуем сформулировать проще?"
        except LLMUnavailable:
            reply = AgentReply(text=OFFLINE_REPLY)
            save_message(db, actor, "assistant", reply.text, channel=channel, payload=reply.to_payload())
            tracing.finish(reply.text)
            return reply

        if not answer:
            answer = _fallback_answer(traces)

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
                      status="done" if result.ok else "failed", summary=result.summary)
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
    return "Готово."


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

def approve_action(db: Session, pending_id: int, actor: User, channel: str = "web") -> ToolResult:
    """Run a prepared action after the human said yes."""
    pending = db.get(PendingAction, pending_id)
    if pending is None or pending.status != "pending":
        return ToolResult(summary="Это действие уже неактуально.", ok=False)

    subject = db.get(User, pending.user_id)
    if subject is None or (actor.id != subject.id and not actor.is_head):
        return ToolResult(summary="Подтвердить это действие может только сам человек.", ok=False)

    spec = registry.get(pending.tool)
    if spec is None:
        pending.status = "expired"
        db.commit()
        return ToolResult(summary="Инструмент больше не доступен.", ok=False)

    arguments = json.loads(pending.arguments_json or "{}")
    image = media.read_and_discard(pending.attachment_path)
    # Действие исполняется в контексте того, чей это был разговор: «да» главы
    # семьи подтверждает чужую просьбу, но не переносит её данные к себе —
    # инструменты знаний ходят по ctx.actor (ADR-0005).
    ctx = ToolContext(db=db, actor=subject, subject=subject, channel=channel,
                      attachments={"image": image} if image else {})
    result = registry.execute(spec, ctx, arguments)

    pending.status = "approved"
    pending.resolved_at = datetime.utcnow()
    pending.result_summary = result.summary[:500]
    db.commit()

    _log_action(db, subject, spec, arguments, result, mode="confirmed")
    save_message(db, actor, "assistant", result.summary, channel=channel,
                 payload=AgentReply(text=result.summary,
                                    traces=[Trace(tool=spec.name, title=spec.title, arguments=arguments,
                                                  status="confirmed", summary=result.summary)],
                                    cards=[result.card] if result.card else []).to_payload())
    return result


def reject_action(db: Session, pending_id: int, actor: User) -> ToolResult:
    pending = db.get(PendingAction, pending_id)
    if pending is None or pending.status != "pending":
        return ToolResult(summary="Это действие уже неактуально.", ok=False)
    if actor.id != pending.user_id and not actor.is_head:
        return ToolResult(summary="Отменить это действие может только сам человек.", ok=False)

    media.read_and_discard(pending.attachment_path)   # вложение больше не нужно
    pending.status = "rejected"
    pending.resolved_at = datetime.utcnow()
    db.commit()
    return ToolResult(summary="Хорошо, не делаю.")


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
