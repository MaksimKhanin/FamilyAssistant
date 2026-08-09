"""Agent tools for memory: remember / recall_notes."""
from datetime import datetime, timedelta

from app.agent.registry import ToolContext, ToolResult, tool
from app.core.events import NOTE_CREATED, bus
from app.core.templating import ru_date
from app.modules.memory import service
from app.modules.memory.models import KIND_LABELS

MODULE = "memory"

#: Rough natural-language deadlines the assistant can turn into an actual time
#: without pulling in a full date parser. Anything else is kept as free text and
#: simply resurfaces «когда придётся к слову».
_RELATIVE = {
    "сегодня": timedelta(hours=4),
    "вечером": timedelta(hours=6),
    "завтра": timedelta(days=1),
    "послезавтра": timedelta(days=2),
    "через неделю": timedelta(days=7),
}


def _parse_when(when: str):
    if not when:
        return None, None
    lowered = when.strip().lower()
    for phrase, delta in _RELATIVE.items():
        if phrase in lowered:
            return when.strip(), datetime.utcnow() + delta
    return when.strip(), None


@tool(
    name="remember",
    module=MODULE,
    title="Запомнить",
    description="""
    Сохранить в память факт о человеке или семье: предпочтение, ограничение по
    здоровью, напоминание или наблюдение. Вызывай, когда человек просит запомнить,
    или когда в разговоре всплыл устойчивый факт, полезный в будущем
    (например «Соня не ест грибы»). Формулируй кратко, в третьем лице.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Короткая формулировка факта"},
            "kind": {"type": "string", "enum": list(KIND_LABELS),
                     "description": "pref — предпочтение, health — здоровье, task — напоминание, fact — наблюдение"},
            "when": {"type": "string",
                     "description": "Когда напомнить, словами: «завтра», «в пятницу утром». Пусто — если это не напоминание."},
        },
        "required": ["text"],
    },
    auto_from=2,
)
def remember(ctx: ToolContext, text: str, kind: str = "fact", when: str = None) -> ToolResult:
    when_text, remind_at = _parse_when(when)
    note = service.add_note(
        ctx.db, ctx.subject.id, text=text, kind=kind,
        source=f"из разговора {ru_date(datetime.now())}",
        when_text=when_text, remind_at=remind_at,
    )
    bus.publish(NOTE_CREATED, {"note_id": note.id, "user_id": ctx.subject.id})

    tail = f" Напомню: {when_text}." if when_text else ""
    return ToolResult(
        summary=f"Запомнил: {note.text}.{tail}",
        data={"note_id": note.id},
        card={
            "type": "memory",
            "note_id": note.id,
            "kind": note.kind,
            "kind_label": KIND_LABELS.get(note.kind, "заметка"),
            "text": note.text,
            "when": when_text,
        },
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

    lines = [f"- [{KIND_LABELS.get(n.kind, n.kind)}] {n.text}" for n in notes]
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
