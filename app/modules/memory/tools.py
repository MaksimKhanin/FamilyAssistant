"""Agent tools for knowledge boards, reminders and rules (tickets #24, #29).

Полномочия ассистента равны полномочиям человека, который с ним говорит:
все инструменты знаний ходят к доскам через `ctx.actor`, а не `ctx.subject` —
экран знаний и доступ ассистента исключены из режима «от лица» (ADR-0005),
иначе человек получал бы чужие доски руками ассистента.

Здесь же живут инструменты, которыми ассистент правит сам себя: правило,
характер, памятка, инструкция доски, а с ADR-0012 ещё и то, о чём он спрашивает
разрешения. Они не про знания, но модуль знаний — единственный, который включён
всегда, а инструмент, меняющий поведение ассистента, не должен исчезать оттого,
что человеку выключили область.
"""
from datetime import datetime, timedelta

from app.agent import policy, registry
from app.agent.registry import ToolContext, ToolResult, tool
from app.core import instructions
from app.core.models import AUTONOMY_LEVELS, MODE_ASK, MODE_AUTO, MODE_OFF
from app.core.templating import ru_datetime
from app.modules.memory import knowledge, reminders, screens, stats
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
    Не сюда идёт то, что касается тебя самого — твоё имя, роль, манера речи
    («тебя зовут Алиса», «будь понастойчивее»): доска не читается сама собой в
    следующем разговоре, а характер — читается всегда. Это set_character.
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
    Поставить напоминание на конкретный момент: «напомни завтра в 9 позвонить
    врачу». Требует абсолютного времени — вычисли дату и время из слов
    человека по текущему моменту из системного промпта («завтра в 9» → конкретная
    дата). Если человек не назвал время или его нельзя понять однозначно —
    не вызывай инструмент, а спроси, когда напомнить.
    Просят повторять («каждый день», «каждый вторник», «каждое 5-е число») —
    передай repeat, а в at — первый раз: «каждый вторник в 9» → ближайший
    вторник 09:00 и repeat=weekly.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "О чём напомнить, коротко"},
            "at": {"type": "string",
                   "description": "Абсолютное местное время «ГГГГ-ММ-ДД ЧЧ:ММ», например «2026-08-15 09:00»"},
            "repeat": {"type": "string", "enum": ["daily", "weekly", "monthly"],
                       "description": "Повторение: каждый день / каждую неделю (день недели из at) "
                                      "/ каждый месяц (число из at). Не передавай для разового."},
        },
        "required": ["text", "at"],
    },
    auto_from=2,
)
def set_reminder(ctx: ToolContext, text: str, at: str, repeat: str = None) -> ToolResult:
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

    reminder = reminders.add_reminder(ctx.db, ctx.subject.id, text=text, remind_at=remind_at,
                                      recurrence=repeat)
    when = ru_datetime(reminder.remind_at)
    if reminder.recurrence:
        when = f"{when}, {reminders.RECURRENCE_WORDS[reminder.recurrence]}"
    return ToolResult(
        summary=f"Напомню ({when}): {reminder.text}",
        data={"reminder_id": reminder.id},
        card={"type": "reminder", "text": reminder.text, "when": when},
    )


@tool(
    name="cancel_reminder",
    module=MODULE,
    title="Снять напоминание",
    description="""
    Снять ещё не сработавшее напоминание: опечатались во времени, поставили
    дважды, передумали. Номер — reminder_id из данных set_reminder или из
    списка на экране «Напоминания». Сработавшее этим инструментом не снять —
    оно уже история, а не план.
    """,
    parameters={
        "type": "object",
        "properties": {
            "reminder_id": {"type": "integer", "description": "Номер напоминания"},
        },
        "required": ["reminder_id"],
    },
    # Необратимо, как drop_rule: время и текст напоминания нигде больше не
    # хранятся, восстановить снятое напоминание неоткуда.
    auto_from=3,
)
def cancel_reminder(ctx: ToolContext, reminder_id: int) -> ToolResult:
    if not reminders.cancel_reminder(ctx.db, ctx.subject.id, reminder_id):
        return ToolResult(
            summary=f"Напоминания #{reminder_id} нет среди ещё не сработавших. "
                    f"Переспроси у человека, какое снять.",
            ok=False,
        )
    return ToolResult(summary=f"Снял напоминание #{reminder_id}.",
                      data={"reminder_id": reminder_id})


@tool(
    name="read_board",
    module=MODULE,
    title="Прочитать доску",
    description="""
    Прочитать содержимое одной доски знаний вместе с её инструкцией: «что было
    за ночь», «покажи лог кормлений». Параметр board — название доски из списка
    в системном промпте. query сужает до записей с подстрокой, period — до
    записей за последние N дней. Используй, когда доска уже понятна по теме
    вопроса или названа в списке досок системного промпта — не трать recall
    впустую. Если неясно, на какой доске искать, начни с recall.
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
    Используй первым шагом, если неясно, на какой доске искать, или вопрос
    затрагивает несколько досок сразу. Если найденного мало, а доска понятна
    из результата — дочитай её через read_board.
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
        having = "; ".join(f"«{t.kind}» — {t.request}" for t in stats.list_tasks(ctx.db, grant.board.id))
        return ToolResult(
            summary=f"На доске «{grant.board.name}» уже пять задач статистики ({having}) — "
                    f"больше не заводится, чтобы сводка не стала отчётом. Переспроси у "
                    f"человека, какую снять (drop_stat), и заведи новую на освободившееся место.",
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
    name="drop_stat",
    module=MODULE,
    title="Снять задачу статистики",
    description="""
    Снять регулярную задачу статистики по доске: «хватит считать воду», «сними
    последнюю задачу». board — название доски, kind — тип величины из словаря
    доски (см. track_board). Табло, заведённое по этой задаче, пропадает вместе
    с ней — своего смысла без задачи у него нет. Снимает автор задачи или
    владелец доски.
    """,
    parameters={
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Название доски"},
            "kind": {"type": "string", "description": "Тип величины, которую больше не считать"},
        },
        "required": ["board", "kind"],
    },
    # Необратимо, как drop_rule: ряд, накопленный задачей, уходит вместе с ней.
    auto_from=3,
)
def drop_stat(ctx: ToolContext, board: str, kind: str) -> ToolResult:
    grant, refusal = _resolve_board(ctx, board)
    if refusal is not None:
        return refusal

    tasks = stats.list_tasks(ctx.db, grant.board.id)
    named = (kind or "").strip().lower()
    matched = [t for t in tasks if t.kind.lower() == named]
    if len(matched) != 1:
        having = "; ".join(f"«{t.kind}» — {t.request}" for t in tasks)
        return ToolResult(
            summary=f"Задачи «{kind}» по доске «{grant.board.name}» не нашёл. "
                    f"Сейчас считается: {having or 'ничего'}. Переспроси у человека, "
                    f"какую снять.",
            ok=False,
        )

    if not stats.delete_task(ctx.db, ctx.actor.id, matched[0].id):
        return ToolResult(
            summary=f"Снять задачу «{matched[0].kind}» может только тот, кто её поставил, "
                    f"или владелец доски «{grant.board.name}».",
            ok=False,
        )
    return ToolResult(summary=f"Снял задачу «{matched[0].kind}» по доске «{grant.board.name}». "
                              f"Табло по ней, если было заведено, тоже пропало.",
                      data={"board_id": grant.board.id, "kind": matched[0].kind})


@tool(
    name="show_stats",
    module=MODULE,
    title="Завести табло",
    description="""
    Завести табло — экран одного показателя в панели по уже поставленной задаче
    статистики: «выведи это на отдельный экран», «покажи кормления столбиками».
    board — название доски, kind — тип величины, если задач по доске несколько.
    name — как назвать экран словами человека: это подпись пункта меню.
    form — вид, выбери его сам под ряд: number — одно число с дельтой (годится
    почти всегда), line — ряд во времени, bars — столбики по дням, table —
    таблица. Своей разметки не придумывай, вида кроме этих четырёх нет.
    Повторный вызов по тому же показателю не заводит второй экран, а меняет
    название и вид, — так и правь табло, когда человек просит показать иначе.
    """,
    parameters={
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Название доски"},
            "name": {"type": "string", "description": "Название экрана — слова человека"},
            "kind": {"type": "string", "description": "Тип величины, если задач по доске несколько"},
            "form": {"type": "string", "enum": ["number", "line", "bars", "table"],
                     "description": "Вид табло: число с дельтой, ряд во времени, столбики, таблица"},
        },
        "required": ["board", "name"],
    },
    # Новый пункт в меню человека — из того же ряда, что доска и регулярная цифра.
    auto_from=3,
)
def show_stats(ctx: ToolContext, board: str, name: str, kind: str = None,
               form: str = None) -> ToolResult:
    grant, refusal = _resolve_board(ctx, board)
    if refusal is not None:
        return refusal

    # Табло растёт из ряда, а ряд — из задачи: считать заново оно не умеет.
    on_board = stats.list_tasks(ctx.db, grant.board.id)
    mine = stats.visible_tasks(ctx.db, ctx.actor.id, [task.id for task in on_board])
    tasks = [task for task in on_board if task.id in mine]
    if not tasks:
        return ToolResult(
            summary=f"По доске «{grant.board.name}» никто ничего не считает — табло "
                    f"показывать нечего. Сначала поставь задачу статистики "
                    f"(track_board), а экран заведёшь по ней.",
            ok=False,
        )

    named = (kind or "").strip().lower()
    matched = [task for task in tasks if task.kind.lower() == named] if named else tasks
    if len(matched) != 1:
        known = "; ".join(f"«{task.kind}» — {task.request}" for task in tasks)
        # Названный тип не нашёлся — это не то же самое, что несколько на выбор:
        # сказать «их несколько», показав один, значит соврать человеку.
        trouble = (f"показателя «{kind}» по доске «{grant.board.name}» не считается"
                   if named else
                   f"по доске «{grant.board.name}» считается несколько показателей")
        return ToolResult(
            summary=f"{trouble.capitalize()}. Считается вот что: {known}. "
                    f"Переспроси у человека, что из этого вывести на экран.",
            ok=False,
        )

    try:
        screen = screens.create_screen(ctx.db, ctx.actor.id, matched[0].id, name, form=form)
    except screens.TooManyScreens:
        having = ", ".join(f"«{s.name}»" for s in screens.list_screens(ctx.db, ctx.actor.id))
        return ToolResult(
            summary=f"У этого человека уже три табло ({having}) — больше в меню не "
                    f"помещается. Скажи ему, что сначала надо снять одно: это делается "
                    f"на самом табло.",
            ok=False,
        )
    if screen is None:
        return ToolResult(summary="Табло без названия не завести: это подпись пункта меню.",
                          ok=False)

    return ToolResult(
        summary=f"Завёл табло «{screen.name}» по доске «{grant.board.name}»: "
                f"{screens.FORMS[screen.form]}. Оно появилось в меню панели.",
        data={"screen_id": screen.id, "task_id": matched[0].id},
        card={"type": "stats-screen", "name": screen.name, "board": grant.board.name,
              "form": screens.FORMS[screen.form], "url": f"/stats/{screen.id}"},
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


# --- правила: чем ассистент правит сам себя --------------------------------------

@tool(
    name="set_rule",
    module=MODULE,
    title="Договориться о правиле",
    description="""
    Запомнить правило — то, что человек велел делать всегда, а не один раз:
    «записывай моё состояние на доску „Самочувствие“ со временем сообщения»,
    «не предлагай сладкое на завтрак». Формулируй правило от второго лица, как
    поручение самому себе, и целиком: через месяц ты прочитаешь только эту строку,
    без разговора вокруг неё. Правило про манеру речи — это set_character, про
    одну доску — set_board_instruction, про целую область — add_memo; сюда идёт
    всё остальное. Поправляя прежнее правило, передай его номер в replaces,
    иначе в реестре останутся два противоречащих.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "Правило одной фразой, от второго лица"},
            "replaces": {"type": "integer",
                         "description": "Номер правила, которое это заменяет"},
        },
        "required": ["text"],
    },
    # Правило переживёт разговор и будет действовать во всех следующих: это тот же
    # ряд, что «запомнить», — пишем в личные данные, при обычной самостоятельности
    # спрашиваем.
    auto_from=2,
)
def set_rule(ctx: ToolContext, text: str, replaces: int = None) -> ToolResult:
    try:
        entry = knowledge.add_rule(ctx.db, ctx.actor.id, text, replaces=replaces)
    except knowledge.TooManyRules:
        having = "; ".join(f"#{number} {line}"
                           for number, line in knowledge.rules_for_prompt(ctx.db, ctx.actor.id))
        # Тупик без выхода: список из двадцати правил без единой ссылки, куда
        # идти чистить реестр, — та же карточка, что у успешного set_rule ниже.
        board = knowledge.rules_board(ctx.db, ctx.actor.id)
        grant = knowledge.board_access(ctx.db, ctx.actor.id, board.id) if board else None
        return ToolResult(
            summary=f"Правил уже {knowledge.RULES_MAX} — больше не заводится, иначе "
                    f"реестр перестанет быть обозримым. Скажи человеку, что сначала "
                    f"надо снять лишнее, и назови, что действует сейчас: {having}",
            ok=False,
            card={"type": "board", "board": board.name, "text": having,
                  "url": knowledge.board_url(grant)} if grant else None,
        )
    if entry is None:
        return ToolResult(summary="Пустое правило не завести.", ok=False)

    board = knowledge.rules_board(ctx.db, ctx.actor.id)
    grant = knowledge.board_access(ctx.db, ctx.actor.id, board.id)
    return ToolResult(
        summary=f"Договорились: {entry.text} (правило #{entry.id}).",
        data={"rule_id": entry.id, "board_id": board.id},
        card={"type": "board", "board": board.name, "text": entry.text,
              "url": knowledge.board_url(grant)},
    )


@tool(
    name="drop_rule",
    module=MODULE,
    title="Снять правило",
    description="""
    Снять действующее правило по его номеру: «больше не записывай моё состояние
    на доску». Номера правил перечислены в системном промпте. Снимай только то,
    о чём человек прямо попросил: правило, которое мешает тебе прямо сейчас, —
    не повод его отменять.
    """,
    parameters={
        "type": "object",
        "properties": {
            "rule_id": {"type": "integer", "description": "Номер правила из системного промпта"},
        },
        "required": ["rule_id"],
    },
    # Необратимо, как «забыть»: человек написал правило словами, и восстановить
    # его формулировку потом неоткуда.
    auto_from=3,
)
def drop_rule(ctx: ToolContext, rule_id: int) -> ToolResult:
    if not knowledge.drop_rule(ctx.db, ctx.actor.id, rule_id):
        return ToolResult(
            summary=f"Правила #{rule_id} нет. Действующие правила перечислены в "
                    f"системном промпте — переспроси у человека, какое снять.",
            ok=False,
        )
    return ToolResult(summary=f"Снял правило #{rule_id} — больше ему не следую.",
                      data={"rule_id": rule_id})


#: Слова, которыми режим называется человеку. Те же, что на экране «Профиль и
#: агент», — ассистент и панель должны говорить об одном одинаково.
_MODE_SAID = {
    MODE_AUTO: "делаю сам, без вопросов",
    MODE_ASK: "готовлю и жду вашего «да»",
    MODE_OFF: "не пользуюсь вовсе",
}


def _known_tools(ctx: ToolContext) -> list:
    """Инструменты, про которые этому человеку есть что настраивать.

    Не `policy.available_tools`: выключенный себе инструмент из того списка
    пропал бы, и включить его обратно словами стало бы нечем — человеку
    пришлось бы идти на экран за тем, что он одной фразой и выключил.
    """
    from app.core.access import is_module_enabled

    return [spec for spec in registry.all_specs()
            if is_module_enabled(ctx.db, ctx.subject.id, spec.module)]


def _resolve_tool(ctx: ToolContext, name: str):
    """Инструмент по имени или по названию из промпта — или отказ со списком."""
    lowered = (name or "").strip().lower()
    known = _known_tools(ctx)
    matched = ([s for s in known if s.name.lower() == lowered]
               or [s for s in known if s.title.lower() == lowered])
    if len(matched) == 1:
        return matched[0], None

    available = ", ".join(f"{s.name} ({s.title})" for s in known)
    return None, ToolResult(
        summary=f"Инструмента «{name}» у этого человека нет. Настроить можно вот "
                f"эти: {available}. Переспроси, что именно он имел в виду.",
        ok=False,
    )


@tool(
    name="set_tool_mode",
    module=MODULE,
    title="Спрашивать или делать",
    description="""
    Задать, как ты обходишься с одним своим инструментом у этого человека:
    auto — делаешь сам, ask — готовишь действие и ждёшь его «да», off — не
    пользуешься вовсе, family — снять его личную настройку и вернуться к тому,
    как задано в доме. Это ответ на «спрашивай меня, прежде чем писать на доски»,
    «не переспрашивай про напоминания» и «больше ничего не записывай сам».
    tool — имя инструмента (log_meal, write_entry), то самое, которым ты его
    вызываешь. Действует только на этого человека и меняет то, как система тебя
    держит, — правилом (set_rule) этого не сделать. Что стоит сейчас, ты видишь
    в системном промпте. Выключенное администратором на весь дом не включается:
    инструмент вернёт отказ, и его надо пересказать человеку.
    """,
    parameters={
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "Имя инструмента, например write_entry"},
            "mode": {"type": "string", "enum": ["auto", "ask", "off", "family"],
                     "description": "auto — сам, ask — спрашивать, off — не пользоваться, "
                                    "family — как задано в доме"},
        },
        "required": ["tool", "mode"],
    },
    # Тот же ряд, что характер и правило: личное, обратимое и видное на экране
    # «Профиль и агент». При обычной самостоятельности человек увидит кнопку «да».
    auto_from=2,
)
def set_tool_mode(ctx: ToolContext, tool: str, mode: str) -> ToolResult:
    spec, refusal = _resolve_tool(ctx, tool)
    if refusal is not None:
        return refusal

    wanted = None if mode == "family" else mode
    try:
        policy.set_own_mode(ctx.db, ctx.subject, spec.name, wanted)
    except policy.LockedByFamily:
        return ToolResult(
            summary=f"«{spec.title}» выключен администратором на всю семью — сам себе "
                    f"человек его вернуть не может. Скажи ему, что это меняется только "
                    f"на админском экране «Агент и инструменты».",
            ok=False,
        )
    except ValueError as e:
        return ToolResult(summary=str(e), ok=False)

    if wanted is None:
        now = policy.dials(ctx.db, ctx.subject).mode(spec)
        return ToolResult(
            summary=f"Снял личную настройку «{spec.title}» — теперь как в доме: "
                    f"{_MODE_SAID.get(now, now)}.",
            data={"tool": spec.name, "mode": now},
        )
    return ToolResult(
        summary=f"Про «{spec.title}» теперь так: {_MODE_SAID[wanted]}. Это только для "
                f"этого человека; поправить можно на экране «Профиль и агент».",
        data={"tool": spec.name, "mode": wanted},
    )


@tool(
    name="set_autonomy",
    module=MODULE,
    title="Задать самостоятельность",
    description="""
    Задать, насколько ты вообще действуешь без спроса у этого человека: 0 — всё
    спрашиваешь, 1 — спрашиваешь про важное, 2 — сам делаешь рутину, 3 —
    максимально самостоятельно. Это ответ на «ничего не делай без спроса» и
    «действуй сам, хватит переспрашивать» — то есть на просьбу сразу про все
    инструменты; про один инструмент — set_tool_mode. follow_family: true снимает
    его личную настройку и возвращает к общей, заданной в доме. Настройка личная:
    у остальных в семье ничего не меняется. Что стоит сейчас, ты видишь в
    системном промпте — не переставляй то, что уже стоит.
    """,
    parameters={
        "type": "object",
        "properties": {
            "level": {"type": "integer", "enum": [0, 1, 2, 3],
                      "description": "0 — всё спрашиваешь, 3 — максимально самостоятельно"},
            "follow_family": {"type": "boolean",
                              "description": "Снять личную настройку и вернуться к общей"},
        },
    },
    # Одна фраза меняет поведение всех инструментов сразу — это ряд «сообщить
    # всей семье»: при обычной самостоятельности спрашиваем, на максимальной нет
    # (там человек уже сказал, что доверяет).
    auto_from=3,
)
def set_autonomy(ctx: ToolContext, level: int = None, follow_family: bool = False) -> ToolResult:
    if follow_family:
        policy.set_own_autonomy(ctx.db, ctx.subject, None)
        now = policy.dials(ctx.db, ctx.subject).autonomy
        return ToolResult(
            summary=f"Снял его личную настройку самостоятельности — теперь как в доме: "
                    f"«{AUTONOMY_LEVELS[now]}».",
            data={"autonomy": now, "own": False},
        )
    if level is None or int(level) not in AUTONOMY_LEVELS:
        return ToolResult(
            summary="Уровень самостоятельности — целое от 0 до 3: 0 — всё спрашиваю, "
                    "3 — максимально самостоятельно. Переспроси у человека, чего он хочет.",
            ok=False,
        )

    policy.set_own_autonomy(ctx.db, ctx.subject, int(level))
    return ToolResult(
        summary=f"Теперь для этого человека: «{AUTONOMY_LEVELS[int(level)]}». Настройка "
                f"его личная — у остальных в семье ничего не изменилось; поправить её "
                f"можно на экране «Профиль и агент».",
        data={"autonomy": int(level), "own": True},
    )


@tool(
    name="set_character",
    module=MODULE,
    title="Задать характер",
    description="""
    Переписать свой характер — то, как ты говоришь с этим человеком и кто ты для
    него: «отвечай суше и без смайликов», «можно неформально, с иронией», «тебя
    зовут Алиса». Сюда идёт манера речи, роль, тон и имя; что делать и куда
    записывать — это set_rule, а не факт о себе через remember: характер читается
    в каждом разговоре, а запись на доске — только по отдельному запросу.
    Текст заменяет прежний характер целиком, поэтому пиши его заново со всем, что
    остаётся в силе, а не одной поправкой. Характер, который тебе задали в промпте,
    ты видишь в блоке «Как ты говоришь».
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "Характер целиком, словами человека: «сухо и по делу»"},
        },
        "required": ["text"],
    },
    # Меняет манеру везде и сразу, но обратимо и видно на экране «Профиль и агент» —
    # тот же ряд, что и запись в личные данные.
    auto_from=2,
)
def set_character(ctx: ToolContext, text: str) -> ToolResult:
    if not (text or "").strip():
        return ToolResult(
            summary="Пустой характер не задать. Человек хочет вернуть привычную "
                    "манеру — он стирает поле сам на экране «Профиль и агент».",
            ok=False,
        )
    # Правим того, за кого работает ассистент: характер — его, а не того, кто
    # разговаривает от его лица.
    updated = instructions.set_character(ctx.db, ctx.subject, text)
    return ToolResult(
        summary=f"Теперь говорю так: «{updated}». Это записано в характер на экране "
                f"«Профиль и агент» — человек может поправить его там.",
        data={"character": updated},
    )


@tool(
    name="add_memo",
    module=MODULE,
    title="Дописать памятку",
    description="""
    Дописать строку в памятку одной области — то, что ты обязан учитывать везде,
    где эта область в деле: «нет желчного, гастрит» про питание, «по средам
    приходит уборщица» про дом. area — название области из списка включённых
    модулей в системном промпте. Это про факты и обстоятельства человека, а не
    про твоё поведение: «записывай состояние на доску» — это set_rule.
    """,
    parameters={
        "type": "object",
        "properties": {
            "area": {"type": "string", "description": "Название области: «Питание», «Знания»"},
            "text": {"type": "string", "description": "Что дописать — одной фразой"},
        },
        "required": ["area", "text"],
    },
    auto_from=2,
)
def add_memo(ctx: ToolContext, area: str, text: str) -> ToolResult:
    from app.core.access import enabled_modules
    from app.modules import by_name, names

    text = (text or "").strip()
    if not text:
        return ToolResult(summary="Пустую строку в памятку не дописать.", ok=False)

    known = by_name()
    allowed = [known[name] for name in enabled_modules(ctx.db, ctx.subject.id, names())
               if name in known and known[name].memo_hint]
    lowered = area.strip().lower()
    matched = ([m for m in allowed if m.title.lower() == lowered]
               or [m for m in allowed if m.name.lower() == lowered]
               or [m for m in allowed if lowered and lowered in m.title.lower()])
    if len(matched) != 1:
        available = ", ".join(f"«{m.title}»" for m in allowed)
        return ToolResult(
            summary=f"Области «{area}» у этого человека нет. Памятку можно писать "
                    f"сюда: {available or 'пока некуда'}.",
            ok=False,
        )

    module = matched[0]
    current = instructions.memo(ctx.db, ctx.subject.id, module.name)
    merged = f"{current}\n{text}".strip() if current else text
    # Не тихая обрезка: памятка — слова человека, и молча укоротить их значит
    # соврать ему о том, что он теперь просил учитывать.
    if len(merged) > instructions.MEMO_LIMIT:
        return ToolResult(
            summary=f"Памятка области «{module.title}» уже заполнена до предела "
                    f"({instructions.MEMO_LIMIT} знаков) — дописать некуда. Скажи "
                    f"человеку, что её надо сократить на экране «Профиль и агент».",
            ok=False,
        )

    instructions.set_memo(ctx.db, ctx.subject.id, module.name, merged)
    return ToolResult(
        summary=f"Дописал в памятку области «{module.title}»: {text}",
        data={"module": module.name},
    )


@tool(
    name="set_board_instruction",
    module=MODULE,
    title="Поправить инструкцию доски",
    description="""
    Переписать инструкцию доски — то, как тебе читать и вести её содержимое:
    «в кормлениях всегда указывай время и объём в миллилитрах». board — название
    доски из системного промпта. Текст заменяет прежнюю инструкцию целиком, а не
    дописывается к ней: прежнюю ты видишь в перечне досок. Инструкцию доски правит
    только её владелец — она меняет твоё поведение для всех, кому доска доступна.
    """,
    parameters={
        "type": "object",
        "properties": {
            "board": {"type": "string", "description": "Название доски"},
            "instruction": {"type": "string",
                            "description": "Инструкция целиком: как читать и вести доску"},
        },
        "required": ["board", "instruction"],
    },
    # Один ряд с заведением доски: остаётся жить и действует на всех допущенных.
    auto_from=3,
)
def set_board_instruction(ctx: ToolContext, board: str, instruction: str) -> ToolResult:
    grant, refusal = _resolve_board(ctx, board)
    if refusal is not None:
        return refusal

    updated = knowledge.update_board(ctx.db, ctx.actor.id, grant.board.id,
                                     name=grant.board.name, instruction=instruction,
                                     section_id=grant.board.section_id)
    if updated is None:
        return ToolResult(
            summary=f"Инструкцию доски «{grant.board.name}» правит только её владелец — "
                    f"этот человек им не является. Скажи ему об этом.",
            ok=False,
        )
    return ToolResult(
        summary=f"Инструкция доски «{grant.board.name}» теперь такая: "
                f"{updated.instruction or 'пусто'}",
        data={"board_id": updated.id},
        card={"type": "board", "board": updated.name,
              "text": updated.instruction or "без инструкции",
              "url": knowledge.board_url(grant)},
    )
