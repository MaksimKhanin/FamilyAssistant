"""Табло — экран одного показателя по задаче статистики (тикет #32, спека #19).

Числа табло считает код — при каждом показе, по самим событиям доски и по
календарным дням семьи (`stats.day_series`, ADR-0013). Раньше экран показывал
ряд, накопленный прогонами сводок, и это было главным источником вранья: пустой
экран при выключенной сводке, вчерашние записи в сегодняшнем столбике,
замороженная первым прогоном цифра. Модели в этих числах по-прежнему нет.
Табло живёт ровно столько, сколько живёт задача за ним, и своего расписания
не заводит.

Вид — выбор из четырёх готовых форм, а не разметка, сочинённая моделью: модель
предлагает, какая из четырёх подходит ряду, человек правит словами или руками.
Сочинённая разметка означала бы, что экран панели каждый раз выглядит по-новому
и может сломаться на пустом ряде.

Пункт навигации у каждого табло свой — его завёл человек, а не код. Это и есть
второй контракт модуля: `nav_items_for(db, user)` рядом со статическим `nav_items`.
"""
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.clock import local_today
from app.core.module import NavItem
from app.core.models import User
from app.modules.memory import knowledge, stats
from app.modules.memory.models import BoardStatsScreen, BoardStatsTask

#: Три табло на человека: навигация панели — не витрина показателей, и пункт,
#: которого не ищут глазами, хуже его отсутствия.
MAX_SCREENS = 3

#: Сколько дней ряда показывает табло. Две недели — столько столбиков читается
#: с телефона, не превращаясь в частокол.
WINDOW_DAYS = 14

#: Столбец `board_stats_screens.name` — String(64).
NAME_LIMIT = 64

#: Четыре готовые формы: что человек выбирает, когда говорит «покажи столбиками».
FORMS = {
    "number": "число с дельтой",
    "line": "ряд во времени",
    "bars": "столбики по дням",
    "table": "таблица",
}
DEFAULT_FORM = "number"


class TooManyScreens(Exception):
    """У человека уже три табло — четвёртое не заводится."""


def _visible(db: Session, user_id: int,
             rows: List[BoardStatsScreen]) -> Dict[int, BoardStatsTask]:
    """Задачи показанных табло, которые этому человеку сейчас видно."""
    return stats.visible_tasks(db, user_id, [row.task_id for row in rows])


def list_screens(db: Session, user_id: int) -> List[BoardStatsScreen]:
    """Табло этого человека — только те, чей ряд ему сейчас видно.

    Право проверяется на каждом показе, а не при заведении: владелец выключил
    рассылку показателя или отозвал доступ к доске — табло пропадает из
    навигации само, ничего не удаляя. Вернёт доступ — вернётся и оно.
    """
    rows = (db.query(BoardStatsScreen)
            .filter(BoardStatsScreen.user_id == user_id)
            .order_by(BoardStatsScreen.id)
            .all())
    if not rows:
        return []
    tasks = _visible(db, user_id, rows)
    # Потолок держится и здесь, а не только при заведении: доступ к ряду могут
    # вернуть, и вчера незаметное табло всплыло бы четвёртым пунктом в меню.
    # Лишним оказывается младшее — старшие человек видел и не снял.
    return [row for row in rows if row.task_id in tasks][:MAX_SCREENS]


def get_screen(db: Session, user_id: int, screen_id: int) -> Optional[BoardStatsScreen]:
    screen = db.get(BoardStatsScreen, screen_id)
    if screen is None or screen.user_id != user_id:
        return None
    return screen if stats.get_task(db, user_id, screen.task_id) is not None else None


def create_screen(db: Session, user_id: int, task_id: int, name: str,
                  form: str = None) -> Optional[BoardStatsScreen]:
    """Завести табло по ряду задачи — или поправить уже заведённое.

    Повторный вызов по тому же ряду не плодит второе табло, а меняет название и
    вид: «покажи это столбиками» — поправка, а не новый экран, и слот из трёх
    она тратить не должна.
    """
    task = stats.get_task(db, user_id, task_id)
    name = (name or "").strip()[:NAME_LIMIT]
    if task is None or not name:
        return None

    mine = list_screens(db, user_id)
    existing = next((s for s in mine if s.task_id == task.id), None)
    if existing is not None:
        existing.name = name
        existing.form = form if form in FORMS else existing.form
        db.commit()
        return existing
    if len(mine) >= MAX_SCREENS:
        raise TooManyScreens()

    screen = BoardStatsScreen(user_id=user_id, task_id=task.id, name=name,
                              form=form if form in FORMS else DEFAULT_FORM)
    db.add(screen)
    db.commit()
    db.refresh(screen)
    return screen


def set_form(db: Session, user_id: int, screen_id: int, form: str) -> bool:
    """Сменить вид табло. Незнакомый вид не меняет ничего: четыре формы — это
    весь выбор, и промах модели не должен оставлять экран без разметки."""
    screen = get_screen(db, user_id, screen_id)
    if screen is None or form not in FORMS:
        return False
    screen.form = form
    db.commit()
    return True


def delete_screen(db: Session, user_id: int, screen_id: int) -> bool:
    """Снять табло. Задача статистики за ним остаётся: цифра в сводке и экран
    в панели — разные обещания, и снимают их по отдельности."""
    screen = get_screen(db, user_id, screen_id)
    if screen is None:
        return False
    db.delete(screen)
    db.commit()
    return True


def nav_items(db: Session, user: User) -> List[NavItem]:
    """Пункты навигации, которых нет в коде: их завёл человек.

    Второй контракт модуля (`nav_items_for`). Считается на каждый переход,
    поэтому здесь один поход за правами на все табло сразу.
    """
    return [NavItem(slug=f"stats-{screen.id}", label=screen.name,
                    url=f"/stats/{screen.id}", icon="chart", short=screen.name[:12])
            for screen in list_screens(db, user.id)]


# --- что показывает табло ---------------------------------------------------------

#: Высота полосы графика в его собственных координатах: столбик или точка
#: рисуются в процентах от неё, поэтому SVG тянется по ширине без пересчёта.
_LINE_HEIGHT = 40
_LINE_PAD = 2


def _line(values: List[float], peak: float) -> str:
    """Точки ломаной для «ряда во времени» — считаются здесь, а не в шаблоне:
    арифметика в разметке не читается и не проверяется тестом.

    Единственная точка рисуется отрезком через всю ширину: ломаная из одной
    точки не рисуется вовсе, и экран выглядел бы пустым при живых данных.
    """
    if not values:
        return ""
    span = _LINE_HEIGHT - 2 * _LINE_PAD
    heights = [_LINE_HEIGHT - _LINE_PAD - value / peak * span for value in values]
    if len(heights) == 1:
        return f"0.0,{heights[0]:.1f} 100.0,{heights[0]:.1f}"
    step = 100 / (len(heights) - 1)
    return " ".join(f"{index * step:.1f},{height:.1f}"
                    for index, height in enumerate(heights))


def screen_view(db: Session, user_id: int, screen: BoardStatsScreen) -> Optional[dict]:
    """Всё, что нужно экрану табло: ряд за окно, последнее число и его дельта.

    Ряд считается по событиям доски на каждый показ (`stats.day_series`,
    ADR-0013) — табло не ждёт прогона сводки и видит поздние записи, правки и
    ответы на плашку уточнения в их собственный день.

    Пустых дней в ряду нет и не выдумывается: за сутки, в которых нечего было
    считать, столбика нет — табло честно говорит «данных за N дней из M», а не
    рисует провал, которого не было (ADR-0002).
    """
    task = stats.get_task(db, user_id, screen.task_id)
    if task is None:
        return None
    series = stats.day_series(db, task, days=WINDOW_DAYS)
    points = series.points
    values = [point["value"] for point in points]
    peak = max(values) if values else 0.0
    # Доска берётся из самого гранта: у права на неё она уже есть, и ходить за
    # ней вторым запросом незачем.
    grant = knowledge.board_access(db, user_id, task.board_id)
    return {
        "screen": screen,
        "task": task,
        "board_name": grant.board.name if grant is not None else "",
        "board_url": knowledge.board_url(grant) if grant is not None else "/memory",
        "form": screen.form if screen.form in FORMS else DEFAULT_FORM,
        "forms": FORMS,
        "unit": series.unit,
        "points": [{"day": point["day"], "value": point["value"],
                    "height": round(point["value"] / peak * 100) if peak else 0}
                   for point in points],
        "line": _line(values, peak or 1.0),
        "last": values[-1] if values else None,
        "delta": values[-1] - values[-2] if len(values) > 1 else None,
        # С чем сравнили: в ряду бывают дыры, и «ко вчерашнему» на разнице с
        # позапрошлой средой было бы неправдой (ADR-0002).
        "delta_from": points[-2]["day"] if len(points) > 1 else None,
        # Последний столбик — сегодняшний, и день ещё идёт: число будет расти,
        # и выдавать его за итог дня нельзя (ADR-0002).
        "today": bool(points) and points[-1]["day"] == local_today(),
        # События задачи, чью единицу точно не пересчитать в единицу ряда: в ось
        # они не легли, но табло их называет, а не теряет молча.
        "stray": series.stray,
        "peak": peak,
        "days_asked": WINDOW_DAYS,
        "days_have": len(points),
        "short": len(points) < WINDOW_DAYS,
    }
