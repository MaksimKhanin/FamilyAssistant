"""Agent tools for memory: remember / recall_notes / set_reminder."""
from datetime import datetime

from app.agent.registry import ToolContext, ToolResult, tool
from app.core.events import NOTE_CREATED, bus
from app.core.templating import ru_date, ru_datetime
from app.modules.memory import reminders, service
from app.modules.memory.models import KIND_TASK, KIND_LABELS

MODULE = "memory"


@tool(
    name="remember",
    module=MODULE,
    title="Запомнить",
    description="""
    Сохранить в память факт о человеке или семье: предпочтение, ограничение по
    здоровью или наблюдение. Вызывай, когда человек просит запомнить, или когда
    в разговоре всплыл устойчивый факт, полезный в будущем (например «Соня не ест
    грибы»). Формулируй кратко, в третьем лице. Для напоминаний со сроком этот
    инструмент не годится — вызывай set_reminder.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Короткая формулировка факта"},
            # «task» намеренно не предлагается: напоминания заводит только
            # set_reminder, с валидным абсолютным временем (спека #19).
            "kind": {"type": "string", "enum": [k for k in KIND_LABELS if k != KIND_TASK],
                     "description": "pref — предпочтение, health — здоровье, fact — наблюдение"},
        },
        "required": ["text"],
    },
    auto_from=2,
)
def remember(ctx: ToolContext, text: str, kind: str = "fact") -> ToolResult:
    note = service.add_note(
        ctx.db, ctx.subject.id, text=text, kind=kind,
        source=f"из разговора {ru_date(datetime.now())}",
    )
    bus.publish(NOTE_CREATED, {"note_id": note.id, "user_id": ctx.subject.id})

    return ToolResult(
        summary=f"Запомнил: {note.text}.",
        data={"note_id": note.id},
        card={
            "type": "memory",
            "note_id": note.id,
            "kind": note.kind,
            "kind_label": KIND_LABELS.get(note.kind, "заметка"),
            "text": note.text,
        },
    )


@tool(
    name="set_reminder",
    module=MODULE,
    title="Напомнить",
    description="""
    Поставить разовое напоминание на конкретный момент: «напомни завтра в 9
    позвонить врачу». Требует абсолютного времени — вычисли дату и время из слов
    человека по текущему моменту из системного промпта («завтра в 9» → конкретная
    дата). Если человек не назвал время или его нельзя понять однозначно —
    не вызывай инструмент, а спроси, когда напомнить.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "О чём напомнить, коротко"},
            "at": {"type": "string",
                   "description": "Абсолютное местное время «ГГГГ-ММ-ДД ЧЧ:ММ», например «2026-08-15 09:00»"},
        },
        "required": ["text", "at"],
    },
    auto_from=2,
)
def set_reminder(ctx: ToolContext, text: str, at: str) -> ToolResult:
    remind_at = reminders.parse_remind_at(at)
    if remind_at is None:
        return ToolResult(
            summary=f"Не разобрал время «{at}» — напоминание не создано. "
                    f"Переспроси у человека, когда именно напомнить.",
            ok=False,
        )
    error = reminders.validate_remind_at(remind_at)
    if error:
        return ToolResult(
            summary=f"{error} Напоминание не создано — переспроси у человека время.",
            ok=False,
        )

    reminder = reminders.add_reminder(ctx.db, ctx.subject.id, text=text, remind_at=remind_at)
    when = ru_datetime(reminder.remind_at)
    return ToolResult(
        summary=f"Напомню ({when}): {reminder.text}",
        data={"reminder_id": reminder.id},
        card={"type": "reminder", "text": reminder.text, "when": when},
    )


@tool(
    name="recall_notes",
    module=MODULE,
    title="Вспомнить",
    description="""
    Найти в памяти, что известно о человеке или семье. Вызывай перед советами о еде,
    планами и покупками, а также когда человек спрашивает «что ты помнишь».
    Параметр query — ключевое слово; без него вернутся последние и закреплённые заметки.
    """,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Ключевое слово для поиска"},
            "kind": {"type": "string", "enum": list(KIND_LABELS)},
        },
    },
    read_only=True,
)
def recall_notes(ctx: ToolContext, query: str = None, kind: str = None) -> ToolResult:
    notes = service.search_notes(ctx.db, ctx.subject.id, query=query, kind=kind)
    if not notes:
        return ToolResult(summary="В памяти пока ничего подходящего нет.",
                          data={"notes": []})

    # Номер в строке — чтобы на «забудь про грибы» было чем сослаться на заметку.
    lines = [f"- #{n.id} [{KIND_LABELS.get(n.kind, n.kind)}] {n.text}" for n in notes]
    return ToolResult(
        summary="Из памяти:\n" + "\n".join(lines),
        data={"notes": [{"id": n.id, "text": n.text, "kind": n.kind} for n in notes]},
        card={
            "type": "recall",
            "notes": [{"id": n.id, "text": n.text, "kind": n.kind,
                       "kind_label": KIND_LABELS.get(n.kind, "заметка"), "pinned": n.pinned}
                      for n in notes[:4]],
        },
    )


@tool(
    name="forget_note",
    module=MODULE,
    title="Забыть заметку",
    description="""
    Убрать из памяти заметку или напоминание: «забудь, что я говорил про грибы»,
    «отмени напоминание про врача». Номер заметки бери из recall_notes; если его нет,
    сначала вызови recall_notes и найди нужную.
    Формулировку, которую человек перестал разделять, лучше забыть, а не переписывать
    поверх: память — не журнал правок.
    """,
    parameters={
        "type": "object",
        "properties": {
            "note_id": {"type": "integer", "description": "Номер заметки из recall_notes"},
        },
        "required": ["note_id"],
    },
    # Как и удаление еды: необратимо, поэтому спрашиваем на всех уровнях, кроме максимального.
    auto_from=3,
)
def forget_note(ctx: ToolContext, note_id: int) -> ToolResult:
    note = service.get_note(ctx.db, ctx.subject.id, note_id)
    if note is None:
        return ToolResult(summary="Такой заметки нет — возможно, её уже забыли.", ok=False)

    text = note.text
    service.forget(ctx.db, ctx.subject.id, note.id)
    return ToolResult(summary=f"Забыл: {text}.", data={"note_id": note_id})
