"""Регулярная статистика доски: задача, прогон и ряд по дням (тикет #31, спека #19).

Разделение труда здесь не вкусовое, а измеренное: прогон прототипа показал, что
свободный подсчёт статистики моделью в контексте ненадёжен. Поэтому **числа
считает код** по событиям доски, а модель получает готовое и только формулирует
фразу по словам, которыми человек задачу и поставил.

Своего расписания у задачи нет: результат приезжает в уже существующую сводку
(`_digest_text` в app/scheduler.py). Второй поток уведомлений семье не нужен.

Тот же прогон дописывает точку в ряд по дням — снимок того, что сводка сказала
в этот день. Табло на снимок не смотрит: свой ряд оно считает по самим событиям
(`day_series`), поэтому поздняя запись и ответ на плашку уточнения доезжают до
экрана, а до уже разосланной сводки — нет (ADR-0013).
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.agent.llm import ROUTINE, LLMClient, LLMUnavailable, client as default_client
from app.agent.prompts import BOARD_STATS_SYSTEM
from app.core.clock import days_ago_start_utc, local_date, local_today, utc_now
from app.core.logging import get_logger
from app.core.models import ScheduledJob, User
from app.core.templating import counted
from app.modules.memory import extraction, knowledge
from app.modules.memory.models import Board, BoardStatsPoint, BoardStatsTask

logger = get_logger("memory.stats")

#: Потолок задач на доску: утренняя сводка не должна превратиться в отчёт.
MAX_TASKS_PER_BOARD = 5

#: В какую сводку приходит результат и какой период она охватывает. Своих
#: расписаний задачи не заводят — только эти три, уже существующие.
WINDOW_DAYS = {"morning_digest": 1, "evening_summary": 1, "weekly_review": 7}
DEFAULT_DIGEST = "morning_digest"

REQUEST_LIMIT = 500
#: Одна фраза в сводке — не абзац: сводку читают с телефона утром.
PHRASE_LIMIT = 400


class TooManyTasks(Exception):
    """На доске уже пять задач статистики — шестая не заводится."""


class NotTheOwner(Exception):
    """Рассылку результата всем допущенным включает только владелец доски."""


# --- постановка задачи ----------------------------------------------------------

def list_tasks(db: Session, board_id: int) -> List[BoardStatsTask]:
    return (db.query(BoardStatsTask)
            .filter(BoardStatsTask.board_id == board_id)
            .order_by(BoardStatsTask.id)
            .all())


def visible_tasks(db: Session, user_id: int,
                  task_ids: Sequence[int]) -> Dict[int, BoardStatsTask]:
    """Из названных задач — те, что этому человеку сейчас видно.

    Видно по двум дорогам, и обе ведут через доску: своя задача на доступной
    доске и чужая, разосланная владельцем всем допущенным. Отобранный доступ
    уносит и задачу — та же граница, что у экранов и у сводки.

    Разрешение считается пачкой: у табло это запрос на каждый переход в панели,
    и ходить за правами по задаче за раз было бы дорого.
    """
    task_ids = list(task_ids)
    if not task_ids:
        return {}
    reachable = {g.board.id for g in knowledge.board_grants(db, user_id)}
    rows = db.query(BoardStatsTask).filter(BoardStatsTask.id.in_(task_ids))
    return {task.id: task for task in rows
            if task.board_id in reachable and (task.author_id == user_id or task.share_all)}


def get_task(db: Session, user_id: int, task_id: int) -> Optional[BoardStatsTask]:
    """Задача, которую этому человеку видно: доска доступна, а задача его
    собственная или разослана владельцем всем допущенным."""
    return visible_tasks(db, user_id, [task_id]).get(task_id)


def create_task(db: Session, user_id: int, board_id: int, request: str, kind: str,
                digest_kind: str = DEFAULT_DIGEST, for_all: bool = False) -> Optional[BoardStatsTask]:
    """Поставить по доске регулярную задачу словами.

    Ставит её всякий, кому доска доступна: вопрос к общему логу не зависит от
    владельца. А вот рассылку результата всем допущенным включает только он —
    рассылать семье уведомления, ни с кем не согласовав, не должен никто.

    Тип берётся из словаря доски: считать «съеденное вообще» коду не по чему.
    """
    grant = knowledge.board_access(db, user_id, board_id)
    request = (request or "").strip()[:REQUEST_LIMIT]
    if grant is None or not request:
        return None
    if for_all and grant.right != knowledge.RIGHT_OWNER:
        raise NotTheOwner()

    known = next((t for t in knowledge.list_event_types(db, board_id)
                  if t.name.lower() == (kind or "").strip().lower()), None)
    if known is None:
        return None
    if len(list_tasks(db, board_id)) >= MAX_TASKS_PER_BOARD:
        raise TooManyTasks()

    task = BoardStatsTask(
        board_id=board_id, author_id=user_id, request=request, kind=known.name,
        digest_kind=digest_kind if digest_kind in WINDOW_DAYS else DEFAULT_DIGEST,
        share_all=bool(for_all),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def set_broadcast(db: Session, user_id: int, task_id: int, on: bool) -> bool:
    """Включить или выключить рассылку результата всем допущенным.

    Только владелец доски — в том числе по задаче, которую поставил не он:
    отвечает за то, что прилетит семье, хозяин лога.
    """
    task = db.get(BoardStatsTask, task_id)
    if task is None:
        return False
    grant = knowledge.board_access(db, user_id, task.board_id)
    if grant is None or grant.right != knowledge.RIGHT_OWNER:
        return False
    task.share_all = bool(on)
    db.commit()
    return True


def delete_task(db: Session, user_id: int, task_id: int) -> bool:
    """Снять задачу — автору или владельцу доски. Ряд уходит вместе с ней."""
    task = db.get(BoardStatsTask, task_id)
    if task is None:
        return False
    grant = knowledge.board_access(db, user_id, task.board_id)
    if grant is None:
        return False
    if task.author_id != user_id and grant.right != knowledge.RIGHT_OWNER:
        return False
    db.delete(task)
    db.commit()
    return True


def digest_is_on(db: Session, user_id: int, digest_kind: str) -> bool:
    """Включена ли у человека та сводка, в которую задача собирается приехать.

    Своего расписания у задачи нет: выключенная сводка — это молчание, и обещать
    человеку ежеутреннюю цифру, не сказав об этом, нельзя.
    """
    return (db.query(ScheduledJob.id)
            .filter(ScheduledJob.user_id == user_id, ScheduledJob.kind == digest_kind,
                    ScheduledJob.enabled.is_(True))
            .first()) is not None


def tasks_for(db: Session, user: User, digest_kind: str) -> List[BoardStatsTask]:
    """Задачи, результат которых приезжает этому человеку в эту сводку.

    Две дороги: своя задача и разосланная владельцем всем допущенным. Обе
    проходят через ту же единственную точку разрешения доступа, что и экраны:
    у кого доски больше нет, тому не приходит и цифра по ней.
    """
    reachable = {g.board.id for g in knowledge.board_grants(db, user.id)}
    if not reachable:
        return []
    rows = (db.query(BoardStatsTask)
            .filter(BoardStatsTask.digest_kind == digest_kind,
                    BoardStatsTask.board_id.in_(reachable))
            .order_by(BoardStatsTask.id))
    return [task for task in rows if task.author_id == user.id or task.share_all]


# --- числа: их считает код ------------------------------------------------------

#: Точные пересчёты единиц — через общую базовую. «0.2 л» и «170 мл» — одна
#: величина, записанная по-разному, и складывать их — арифметика, а не догадка.
#: Всего, чего в таблице нет, пересчёт не касается: приблизительный курс хуже
#: двух честных строк.
_UNIT_BASES = {
    "мл": ("мл", 1.0), "л": ("мл", 1000.0),
    "ml": ("мл", 1.0), "l": ("мл", 1000.0),
    "мг": ("г", 0.001), "г": ("г", 1.0), "кг": ("г", 1000.0),
    "mg": ("г", 0.001), "g": ("г", 1.0), "kg": ("г", 1000.0),
    "с": ("с", 1.0), "сек": ("с", 1.0), "мин": ("с", 60.0),
    "ч": ("с", 3600.0), "час": ("с", 3600.0),
    "см": ("м", 0.01), "м": ("м", 1.0), "км": ("м", 1000.0),
    "km": ("м", 1000.0), "m": ("м", 1.0),
}


def convert_unit(value: float, unit: Optional[str], target: Optional[str]) -> Optional[float]:
    """Точный пересчёт величины в другую единицу — или None, когда пересчёта нет.

    Совпадающие единицы (с точностью до регистра) проходят как есть; дальше —
    только пары из таблицы с общей базовой. «шт» в «мл» не пересчитывается
    ничем, и None здесь — честный ответ, а не ошибка.
    """
    given, wanted = (unit or "").strip().lower(), (target or "").strip().lower()
    if given == wanted:
        return value
    base_given, base_wanted = _UNIT_BASES.get(given), _UNIT_BASES.get(wanted)
    if base_given is None or base_wanted is None or base_given[0] != base_wanted[0]:
        return None
    return value * base_given[1] / base_wanted[1]


@dataclass
class Figures:
    """Посчитанное по задаче: суммы по единицам, основная строка первой.

    Единица не сливается: «170 мл» и «0.2 л» — две строки, а сумма, неизвестно
    в чём, хуже двух известных. Основная — та, что в единице словаря доски.

    В ряд по дням идёт только основная строка: ряд — это один показатель в одной
    единице, иначе табло рисовало бы миллилитры и литры на одной оси. Прочие
    строки не теряются — их называет фраза сводки.
    """
    kind: str
    days: int
    rows: List[dict]

    @property
    def counted(self) -> bool:
        return bool(self.rows)

    @property
    def total(self) -> float:
        return self.rows[0]["total"] if self.rows else 0.0

    @property
    def unit(self) -> Optional[str]:
        return self.rows[0]["unit"] if self.rows else None


def window(task: BoardStatsTask, now: datetime):
    """Период задачи: сколько назад смотреть от момента прогона."""
    days = WINDOW_DAYS.get(task.digest_kind, WINDOW_DAYS[DEFAULT_DIGEST])
    return now - timedelta(days=days), now, days


def _canonical_unit(db: Session, task: BoardStatsTask) -> Optional[str]:
    """Единица словаря доски для типа задачи — ось, к которой приводятся суммы."""
    known = next((t for t in knowledge.list_event_types(db, task.board_id)
                  if t.name.lower() == task.kind.lower()), None)
    return known.unit if known is not None else None


def figures(db: Session, task: BoardStatsTask, since: datetime, until: datetime,
            days: int = 1) -> Figures:
    """Суммы по задаче за период — по событиям, а не по тексту записей.

    Неуверенно разобранное в цифру не идёт, пока человек не уточнил: цифра,
    которой нельзя верить, хуже отсутствующей.

    Литры складываются с миллилитрами: точный пересчёт в единицу словаря — та же
    арифметика кода, что и сумма. Отдельной строкой остаётся только то, чего
    точно не пересчитать.
    """
    canonical = _canonical_unit(db, task)
    rows: dict = {}
    for event in knowledge.board_events(db, task.board_id, since, until):
        if event.kind.lower() != task.kind.lower() or event.confidence == extraction.LOW:
            continue
        converted = convert_unit(event.value, event.unit, canonical)
        value, unit = ((converted, canonical) if converted is not None
                       else (event.value, event.unit))
        row = rows.setdefault(unit, {"unit": unit, "total": 0.0, "count": 0})
        row["total"] += value
        row["count"] += 1

    ordered = sorted(rows.values(),
                     key=lambda row: (row["unit"] != canonical, -row["count"], row["unit"] or ""))
    return Figures(kind=task.kind, days=days, rows=ordered)


def waiting_for_clarification(db: Session, board_id: int, since: datetime,
                              until: datetime) -> int:
    """Сколько записей доски за период ждут уточнения.

    Их величины в цифру не вошли, и человеку стоит знать об этом одной фразой —
    неполное не выдаётся за полное (ADR-0002).
    """
    return len({event.entry_id for event in knowledge.board_events(db, board_id, since, until)
                if event.confidence == extraction.LOW})


# --- фраза: её формулирует модель по готовым числам -------------------------------

def _window_words(days: int) -> str:
    if days <= 1:
        return "за сутки"
    if days == 7:
        return "за неделю"
    return f"за {counted(days, 'день', 'дня', 'дней')}"


def _value(row: dict) -> str:
    return f"{row['total']:g}" + (f" {row['unit']}" if row["unit"] else "")


def plain(board_name: str, numbers: Figures) -> str:
    """Фраза, собранная кодом, — на случай, если модель промолчала.

    Цифра важнее формулировки: сводка без красивых слов полезна, сводка без
    числа — нет.
    """
    values = ", ".join(_value(row) for row in numbers.rows)
    return f"«{board_name}», {numbers.kind} {_window_words(numbers.days)}: {values}."


def _numbers_block(numbers: Figures) -> str:
    return "\n".join(
        f"- {numbers.kind}: {_value(row)}"
        + (f", событий {row['count']}" if row["count"] is not None else "")
        for row in numbers.rows)


def phrase(request: str, board_name: str, numbers: Figures, llm: LLMClient = None) -> str:
    """Одна фраза для сводки по уже посчитанным числам."""
    llm = llm or default_client
    prompt = (
        f"Доска: «{board_name}».\n"
        f"Задача этого человека его словами: {request}\n"
        f"Период: {_window_words(numbers.days)}.\n\n"
        f"Посчитано кодом:\n{_numbers_block(numbers)}"
    )
    # Числа уже посчитаны кодом (ADR-0002) — от модели тут одна фраза по готовому.
    # Думать над ней не о чем, и раньше эта задача молча ехала на ручке оценок.
    raw = llm.json_completion(BOARD_STATS_SYSTEM, prompt, task=ROUTINE)
    return str(raw.get("text") or "").strip()[:PHRASE_LIMIT]


def safe_phrase(request: str, board_name: str, numbers: Figures,
                llm: LLMClient = None) -> str:
    """То же, но никогда не падает и никогда не молчит."""
    try:
        said = phrase(request, board_name, numbers, llm=llm)
        if said:
            return said
        logger.warning("Модель не сформулировала фразу — говорю числами")
    except LLMUnavailable:
        logger.warning("Модель недоступна — цифра в сводке будет без формулировки")
    except (AttributeError, TypeError, ValueError) as error:
        logger.warning(f"Не разобрал ответ модели по задаче статистики: {error}")
    return plain(board_name, numbers)


# --- прогон и ряд по дням ---------------------------------------------------------

def point_of_day(db: Session, task_id: int, day: date) -> Optional[BoardStatsPoint]:
    return (db.query(BoardStatsPoint)
            .filter(BoardStatsPoint.task_id == task_id, BoardStatsPoint.day == day)
            .one_or_none())


def record_point(db: Session, task: BoardStatsTask, day: date, value: float,
                 unit: str = None) -> BoardStatsPoint:
    """Точка ряда за календарный день семьи.

    День в ряду один: повторный прогон уточняет точку, а не плодит их.
    """
    point = point_of_day(db, task.id, day)
    if point is None:
        point = BoardStatsPoint(task_id=task.id, day=day, value=value, unit=unit)
        db.add(point)
    else:
        point.value, point.unit = value, unit
    db.commit()
    return point


def run_task(db: Session, task: BoardStatsTask, now: datetime = None,
             llm: LLMClient = None) -> Optional[str]:
    """Прогон задачи: посчитать, дописать точку в ряд и сформулировать фразу.

    День точки — день прогона, а не календарные сутки данных: задача смотрит
    назад от своего момента («сколько съел за сутки» утром — это и вчерашний
    вечер, и ночь), и точка ряда значит «вот что задача сказала в этот день».

    Считать нечего — не фраза и не точка: пустые сутки не ноль, и табло потом
    честно скажет «данных за 2 дня из 7», а не нарисует провал, которого не было.

    Цифра за день считается один раз. Разосланную владельцем задачу прогоняет
    сводка каждого допущенного, и сводки эти приходят в разное время, — но
    второй услышит ровно то же число, что и первый, а точка ряда не перепишется
    задним числом (ADR-0002).
    """
    now = now or utc_now()
    day = local_date(now)
    days = window(task, now)[2]

    recorded = point_of_day(db, task.id, day)
    if recorded is not None:
        numbers = Figures(kind=task.kind, days=days,
                          rows=[{"unit": recorded.unit, "total": recorded.value, "count": None}])
    else:
        since, until, _ = window(task, now)
        numbers = figures(db, task, since, until, days=days)
        if not numbers.counted:
            return None
        # Точка — первой: число переживёт и молчащую модель.
        record_point(db, task, day, numbers.total, numbers.unit)

    board = db.get(Board, task.board_id)
    return safe_phrase(task.request, board.name if board else "", numbers, llm=llm)


def series(db: Session, task_id: int, days: int = None) -> List[BoardStatsPoint]:
    """Снимки прогонов по дням — что задача сказала в сводке в какой день."""
    rows = db.query(BoardStatsPoint).filter(BoardStatsPoint.task_id == task_id)
    if days:
        rows = rows.filter(BoardStatsPoint.day > local_today() - timedelta(days=days))
    return rows.order_by(BoardStatsPoint.day).all()


# --- ряд табло: по календарным дням из самих событий ------------------------------

@dataclass
class DaySeries:
    """Ряд по календарным дням семьи, посчитанный по событиям доски при показе.

    `points` — по строке на день, в котором было что считать; пустые дни не
    выдумываются нулями. `stray` — события задачи, чью единицу точно не
    пересчитать в единицу ряда: в ось они не легли, но названы, а не потеряны.
    """
    unit: Optional[str]
    points: List[dict] = field(default_factory=list)   # {day, value, count}
    stray: List[dict] = field(default_factory=list)    # {unit, total, count}


def day_series(db: Session, task: BoardStatsTask, days: int) -> DaySeries:
    """Ряд табло за последние `days` календарных дней — прямо из событий.

    Считается кодом при каждом показе, а не копится прогонами сводок: поздняя
    запись, правка и ответ на плашку уточнения меняют вчерашний столбик, потому
    что изменились сами данные. Пересчитывать тут нечего наугад — разбор записи
    на события по-прежнему один, при записи (ADR-0013).

    День у события свой (`at`), а не день прогона: цифра «за 16-е» — это события
    16-го числа календаря семьи, что бы и когда бы по ним ни говорила сводка.
    """
    canonical = _canonical_unit(db, task)
    per_day: Dict[date, dict] = {}
    stray: Dict[Optional[str], dict] = {}
    for event in knowledge.board_events(db, task.board_id, days_ago_start_utc(days - 1)):
        if event.kind.lower() != task.kind.lower() or event.confidence == extraction.LOW:
            continue
        value = convert_unit(event.value, event.unit, canonical)
        if value is None:
            row = stray.setdefault(event.unit, {"unit": event.unit, "total": 0.0, "count": 0})
            row["total"] += event.value
            row["count"] += 1
            continue
        day = local_date(event.at)
        bucket = per_day.setdefault(day, {"day": day, "value": 0.0, "count": 0})
        bucket["value"] += value
        bucket["count"] += 1
    return DaySeries(unit=canonical,
                     points=[per_day[day] for day in sorted(per_day)],
                     stray=[stray[unit] for unit in sorted(stray, key=lambda u: u or "")])


# --- то, что видно в сводке --------------------------------------------------------

def digest_parts(db: Session, user: User, digest_kind: str, now: datetime = None,
                 llm: LLMClient = None) -> List[str]:
    """Часть сводки этого человека: цифры по его задачам и одна фраза про
    необъяснённое.

    Прогон один на задачу и на день (ряд держит точку по ключу «задача + день»),
    а вот фразу каждому получателю модель формулирует свою: сводки участников
    семьи приходят в разное время, и общего места для готовой фразы у них нет.
    """
    now = now or utc_now()
    tasks = tasks_for(db, user, digest_kind)
    parts = [said for said in (run_task(db, task, now=now, llm=llm) for task in tasks) if said]
    waiting = _waiting_line(db, user, tasks, now)
    if waiting:
        parts.append(waiting)
    return parts


def _waiting_line(db: Session, user: User, tasks: List[BoardStatsTask],
                  now: datetime) -> Optional[str]:
    """«…и 2 записи ждут уточнения» со ссылкой — чтобы знать про необъяснённое,
    не разбирая ленту."""
    total, url = 0, None
    for board_id in dict.fromkeys(task.board_id for task in tasks):
        widest = max(window(task, now)[2] for task in tasks if task.board_id == board_id)
        count = waiting_for_clarification(db, board_id, now - timedelta(days=widest), now)
        if not count:
            continue
        total += count
        if url is None:
            url = knowledge.board_url(knowledge.board_access(db, user.id, board_id))
    if not total:
        return None
    return f"…и {counted(total, 'запись ждёт', 'записи ждут', 'записей ждут')} уточнения: {url}"
