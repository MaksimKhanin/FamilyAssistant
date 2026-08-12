"""Agent tools for knowledge boards and reminders (tickets #24, #29).

Полномочия ассистента равны полномочиям человека, который с ним говорит:
все инструменты знаний ходят к доскам через `ctx.actor`, а не `ctx.subject` —
экран знаний и доступ ассистента исключены из режима «от лица» (ADR-0005),
иначе глава семьи получал бы чужие доски руками ассистента.
"""
from datetime import datetime, timedelta

from app.agent.registry import ToolContext, ToolResult, tool
from app.core.templating import ru_datetime
from app.modules.memory import knowledge, reminders, stats
from app.modules.memory.models import RIGHT_VIEW

MODULE = "memory"


def _ambiguous(kind: str, matches) -> ToolResult:
    names = ", ".join(f"«{g.board.name}»" for g in matches)
    return ToolResult(
        summary=f"Название {kind} неоднозначно, подходят: {names}. "
                f"Переспроси у человека, какая имеется в виду.",
        ok=False,
    )


def _resolve_board(ctx: ToolContext, name: str):
    """Доска по имени — или готовый ToolResult с отказом вместо догадки."""
    matches = knowledge.find_boards_by_name(ctx.db, ctx.actor.id, name)
    if not matches:
        available = ", ".join(f"«{g.board.name}»"
                              for g in knowledge.board_grants(ctx.db, ctx.actor.id))
        return None, ToolResult(
            summary=f"Доски «{name}» не нашёл. Доступные доски: {available or 'пока нет'}.",
            ok=False,
        )
    if len(matches) > 1:
        return None, _ambiguous("доски", matches)
    return matches[0], None


def _author_label(entry, names: dict) -> str:
    if entry.by_assistant:
        return "Ассистент"
    if entry.author_id is None:
        return "бывший участник"
    return names.get(entry.author_id, "участник")


@tool(
    name="remember",
    module=MODULE,
    title="Запомнить",
    description="""
    Записать себе на личную доску «Память ассистента» устойчивый факт о человеке
    или семье: предпочтение, ограничение по здоровью, наблюдение («Соня не ест
    грибы»). Вызывай, когда человек просит запомнить или когда факт пригодится
    в будущем. Формулируй кратко, в третьем лице. Для напоминаний со сроком —
    set_reminder; для записи на конкретную доску по просьбе человека — write_entry.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Короткая формулировка факта"},
        },
        "required": ["text"],
    },
    auto_from=2,
)
def remember(ctx: ToolContext, text: str) -> ToolResult:
    if not text.strip():
        # До заведения доски: пустая формулировка не повод оставить за собой
        # пустую «Память ассистента» на экране знаний.
        return ToolResult(summary="Пустую формулировку не запомнить.", ok=False)
    # Доска заводится лениво при первом запоминании (спека #19).
    board = knowledge.assistant_board(ctx.db, ctx.actor.id)
    entry = knowledge.add_assistant_entry(ctx.db, ctx.actor.id, board.id, text)
    if entry is None:
        return ToolResult(summary="Пустую формулировку не запомнить.", ok=False)

    grant = knowledge.board_access(ctx.db, ctx.actor.id, board.id)
    return ToolResult(
        summary=f"Запомнил: {entry.text}.",
        data={"entry_id": entry.id, "board_id": board.id},
        card={"type": "board", "board": board.name, "text": entry.text,
              "url": knowledge.board_url(grant)},
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
    name="read_board",
    module=MODULE,
    title="Прочитать доску",
    description="""
    Прочитать содержимое одной доски знаний вместе с её инструкцией: «что было
    за ночь», «покажи лог кормлений». Параметр board — название доски из списка
    в системном промпте. query сужает до записей с подстрокой, period — до
    записей за последние N дней.
    """,
    parameters={
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Название доски"},
            "query": {"type": "string", "description": "Подстрока для отбора записей"},
            "period": {"type": "integer", "description": "Сколько последних дней показать"},
        },
        "required": ["board"],
    },
    read_only=True,
)
def read_board(ctx: ToolContext, board: str, query: str = None, period: int = None) -> ToolResult:
    grant, refusal = _resolve_board(ctx, board)
    if refusal is not None:
        return refusal

    entries = knowledge.list_entries(ctx.db, ctx.actor.id, grant.board.id)
    if period:
        floor = datetime.utcnow() - timedelta(days=period)
        entries = [e for e in entries if e.created_at >= floor]
    if query:
        lowered = query.strip().lower()
        entries = [e for e in entries if lowered in e.text.lower()]
    entries = entries[-100:]   # хвост: контекст модели не резиновый

    names = {m.id: m.display_name for m in ctx.actor.family.members} if ctx.actor.family else {}
    lines = [f"#{e.id} [{_author_label(e, names)}] {ru_datetime(e.created_at)}: {e.text}"
             for e in entries]
    instruction = (f"Инструкция доски: {grant.board.instruction}\n"
                   if grant.board.instruction else "")
    body = "\n".join(lines) if lines else "Записей нет."
    return ToolResult(
        summary=f"Доска «{grant.board.name}».\n{instruction}{body}",
        data={"board_id": grant.board.id,
              "entries": [{"id": e.id, "text": e.text} for e in entries]},
    )


@tool(
    name="recall",
    module=MODULE,
    title="Вспомнить",
    description="""
    Найти по всем доступным доскам знаний, что известно о человеке или семье:
    перед советами о еде, планами и покупками, и когда человек спрашивает
    «что ты помнишь». query — ключевое слово; в выдаче указана доска, откуда факт.
    """,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Ключевое слово для поиска"},
        },
        "required": ["query"],
    },
    read_only=True,
)
def recall(ctx: ToolContext, query: str) -> ToolResult:
    hits = knowledge.search_entries(ctx.db, ctx.actor.id, query)
    if not hits:
        return ToolResult(summary="На досках пока ничего подходящего нет.",
                          data={"hits": []})

    # Номер в строке — чтобы на «забудь про грибы» было чем сослаться на запись.
    lines = [f"- #{e.id} (доска «{b.name}») {e.text}" for e, b in hits]
    return ToolResult(
        summary="Нашёл на досках:\n" + "\n".join(lines),
        data={"hits": [{"id": e.id, "text": e.text, "board": b.name} for e, b in hits]},
        card={
            "type": "board-recall",
            "hits": [{"board": b.name, "text": e.text} for e, b in hits[:4]],
        },
    )


@tool(
    name="write_entry",
    module=MODULE,
    title="Записать на доску",
    description="""
    Добавить запись в ленту доски по просьбе человека: «запиши в кормления:
    170 мл в 2:50». Параметр board — название доски из системного промпта.
    Пиши на доски только по явной просьбе; для собственных наблюдений — remember.
    Если название доски неоднозначно, инструмент вернёт варианты — переспроси.
    """,
    parameters={
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Название доски"},
            "text": {"type": "string", "description": "Текст записи — слова человека, не пересказ"},
        },
        "required": ["board", "text"],
    },
    auto_from=2,
)
def write_entry(ctx: ToolContext, board: str, text: str) -> ToolResult:
    grant, refusal = _resolve_board(ctx, board)
    if refusal is not None:
        return refusal
    if grant.right == RIGHT_VIEW:
        return ToolResult(
            summary=f"На доске «{grant.board.name}» у этого человека только просмотр — "
                    f"писать туда нельзя.",
            ok=False,
        )

    entry = knowledge.add_entry(ctx.db, ctx.actor.id, grant.board.id, text)
    if entry is None:
        return ToolResult(summary="Пустую запись не сохранить.", ok=False)
    return ToolResult(
        summary=f"Записал на доску «{grant.board.name}»: {entry.text}",
        data={"entry_id": entry.id, "board_id": grant.board.id},
        card={"type": "board", "board": grant.board.name, "text": entry.text,
              "url": knowledge.board_url(grant)},
    )


@tool(
    name="create_board",
    module=MODULE,
    title="Завести доску",
    description="""
    Завести новую доску в разделе человека: «заведи мне доску под показания
    счётчиков». section — название существующего раздела; instruction — как
    читать и вести доску (предложи её сам из слов человека). Удалять и
    переименовывать доски и разделы ты не умеешь — это делается руками в панели.
    """,
    parameters={
        "type": "object",
        "properties": {
            "section": {"type": "string", "description": "Название раздела"},
            "name": {"type": "string", "description": "Название новой доски"},
            "instruction": {"type": "string",
                            "description": "Инструкция ассистенту: как читать и вести доску"},
        },
        "required": ["section", "name"],
    },
    # Новая сущность в знаниях человека — по умолчанию спрашиваем.
    auto_from=3,
)
def create_board(ctx: ToolContext, section: str, name: str, instruction: str = None) -> ToolResult:
    sections = knowledge.find_sections_by_name(ctx.db, ctx.actor.id, section)
    if not sections:
        available = ", ".join(f"«{s.name}»"
                              for s in knowledge.list_sections(ctx.db, ctx.actor.id))
        return ToolResult(
            summary=f"Раздела «{section}» нет. Разделы этого человека: "
                    f"{available or 'пока нет — их заводят в панели'}.",
            ok=False,
        )
    if len(sections) > 1:
        names = ", ".join(f"«{s.name}»" for s in sections)
        return ToolResult(summary=f"Название раздела неоднозначно, подходят: {names}. "
                                  f"Переспроси у человека.", ok=False)

    board = knowledge.create_board(ctx.db, ctx.actor.id, sections[0].id, name, instruction)
    if board is None:
        return ToolResult(summary="Доску с пустым названием не завести.", ok=False)
    grant = knowledge.board_access(ctx.db, ctx.actor.id, board.id)
    return ToolResult(
        summary=f"Завёл доску «{board.name}» в разделе «{sections[0].name}».",
        data={"board_id": board.id},
        card={"type": "board", "board": board.name,
              "text": board.instruction or "без инструкции",
              "url": knowledge.board_url(grant)},
    )


#: Сводки, в которые может приехать регулярная цифра, — те же, что уже есть у
#: семьи: «где скажу» и как эта сводка называется.
DIGEST_LABELS = {
    "morning_digest": ("в утренней сводке", "утренняя сводка"),
    "evening_summary": ("в вечернем итоге", "вечерний итог"),
    "weekly_review": ("в разборе недели", "разбор недели"),
}


@tool(
    name="track_board",
    module=MODULE,
    title="Считать по доске",
    description="""
    Поставить по доске регулярную задачу статистики: «каждое утро говори, сколько
    малыш съел за сутки». Числа посчитает код по событиям доски — ты только
    передаёшь просьбу человека его словами (request) и тип величины из словаря
    доски (kind). Не знаешь типов доски — вызови инструмент без kind: он вернёт
    словарь, и тогда переспроси, что именно считать. digest — в какую из
    существующих сводок это приезжает; своего расписания у задачи нет.
    for_all — рассылать результат всем допущенным, и это можно только владельцу
    доски: без явной просьбы владельца не включай.
    """,
    parameters={
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Название доски"},
            "request": {"type": "string",
                        "description": "Что считать — словами человека, а не пересказом"},
            "kind": {"type": "string", "description": "Тип величины из словаря доски"},
            "digest": {"type": "string",
                       "enum": ["morning_digest", "evening_summary", "weekly_review"],
                       "description": "В какую сводку: утреннюю, вечернюю или недельную"},
            "for_all": {"type": "boolean",
                        "description": "Рассылать всем допущенным — только по просьбе владельца доски"},
        },
        "required": ["board", "request"],
    },
    # Регулярная цифра в сводке — новая привычка ассистента, а не разовый ответ:
    # по умолчанию спрашиваем, как и при заведении доски.
    auto_from=3,
)
def track_board(ctx: ToolContext, board: str, request: str, kind: str = None,
                digest: str = None, for_all: bool = False) -> ToolResult:
    grant, refusal = _resolve_board(ctx, board)
    if refusal is not None:
        return refusal

    types = knowledge.list_event_types(ctx.db, grant.board.id)
    if not types:
        return ToolResult(
            summary=f"У доски «{grant.board.name}» нет словаря величин — считать не по чему. "
                    f"Скажи человеку, что типы величин заводятся на самой доске в панели.",
            ok=False,
        )
    known = ", ".join(f"«{t.name}»" + (f" ({t.unit})" if t.unit else "") for t in types)
    # Тип не назван, а величина у доски одна — переспрашивать не о чем.
    kind = (kind or "").strip() or (types[0].name if len(types) == 1 else "")
    if not kind or kind.lower() not in {t.name.lower() for t in types}:
        return ToolResult(
            summary=f"Словарь величин доски «{grant.board.name}»: {known}. "
                    f"Переспроси у человека, что из этого считать.",
            ok=False,
        )

    try:
        task = stats.create_task(ctx.db, ctx.actor.id, grant.board.id, request=request,
                                 kind=kind, digest_kind=digest or stats.DEFAULT_DIGEST,
                                 for_all=bool(for_all))
    except stats.NotTheOwner:
        return ToolResult(
            summary=f"Рассылать результат всем допущенным может только владелец доски "
                    f"«{grant.board.name}». Поставь задачу без рассылки или передай "
                    f"просьбу владельцу.",
            ok=False,
        )
    except stats.TooManyTasks:
        return ToolResult(
            summary=f"На доске «{grant.board.name}» уже пять задач статистики — больше "
                    f"не заводится, чтобы сводка не стала отчётом. Скажи человеку, что "
                    f"сначала надо снять лишнюю.",
            ok=False,
        )
    if task is None:
        return ToolResult(summary="Задачу с пустой просьбой не поставить.", ok=False)

    where, named = DIGEST_LABELS.get(task.digest_kind, DIGEST_LABELS[stats.DEFAULT_DIGEST])
    audience = " Результат увидят все допущенные к доске." if task.share_all else ""
    # Своего расписания у задачи нет: выключенная сводка — это молчание, и
    # обещать человеку ежеутреннюю цифру, не сказав об этом, нельзя.
    silent = ("" if stats.digest_is_on(ctx.db, ctx.actor.id, task.digest_kind)
              else f" Предупреди человека: {named} у него сейчас выключена, и цифра "
                   f"начнёт приходить, когда он включит её в настройках.")
    return ToolResult(
        summary=f"Буду считать «{task.kind}» по доске «{grant.board.name}» и говорить "
                f"{where}: {task.request}.{audience}{silent}",
        data={"task_id": task.id, "board_id": grant.board.id},
        card={"type": "board", "board": grant.board.name, "text": task.request,
              "url": knowledge.board_url(grant)},
    )


@tool(
    name="forget",
    module=MODULE,
    title="Забыть",
    description="""
    Удалить свою запись с доски «Память ассистента»: «забудь, что я говорил про
    грибы». Номер записи бери из recall или read_board; если его нет — сначала
    найди. Чужие записи — записи людей — ты не удаляешь и не правишь никогда.
    """,
    parameters={
        "type": "object",
        "properties": {
            "entry_id": {"type": "integer", "description": "Номер записи из recall"},
        },
        "required": ["entry_id"],
    },
    # Как и удаление еды: необратимо, поэтому спрашиваем на всех уровнях, кроме максимального.
    auto_from=3,
)
def forget(ctx: ToolContext, entry_id: int) -> ToolResult:
    removed = knowledge.delete_assistant_entry(ctx.db, ctx.actor.id, entry_id)
    if not removed:
        return ToolResult(
            summary="Эту запись удалить нельзя: либо её нет, либо она написана "
                    "человеком — чужие записи ассистент не трогает.",
            ok=False,
        )
    return ToolResult(summary="Забыл — запись удалена.", data={"entry_id": entry_id})
