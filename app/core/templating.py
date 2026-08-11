"""Single Jinja2 environment shared by the core and every module.

Templates live under app/templates/, each module keeping its own subdirectory
(templates/nutrition/..., templates/security/...) and extending the shared base.
"""
import hashlib
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core.clock import local_now, to_local

_APP_DIR = Path(__file__).resolve().parent.parent

templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))


def _asset_version() -> str:
    """Отпечаток статики, «?v=…» в ссылках на неё.

    Service worker отдаёт статику из кеша и обновляет её в фоне — значит, без
    версии первое открытие после деплоя получает свежую разметку со вчерашним
    app.js. Пока разметка от него не зависела, это было безвредно; с переходами
    через hx-boost — ломает меню и чат до следующего открытия. Версия в URL
    решает это конструкцией: новая разметка ссылается на адрес, которого в
    кеше ещё нет.
    """
    digest = hashlib.sha256()
    for name in sorted(p.name for p in (_APP_DIR / "static").glob("*.*")):
        path = _APP_DIR / "static" / name
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


templates.env.globals["asset_v"] = _asset_version()


def render(request, name: str, context: dict = None, status_code: int = 200):
    """Render a template. One wrapper so route code is not tied to Starlette's signature."""
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)

_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
           "июля", "августа", "сентября", "октября", "ноября", "декабря")
_WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")


def plural(count: int, one: str, few: str, many: str) -> str:
    """Russian pluralisation: 1 заметка / 2 заметки / 5 заметок."""
    n = abs(int(count))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def counted(count: int, one: str, few: str, many: str) -> str:
    return f"{count} {plural(count, one, few, many)}"


def genitive(name: str) -> str:
    """Rough genitive of a Russian first name — «для Лёвы», «для Марины», «для Артёма».

    Deliberately simple: the UI only ever uses it with names the family typed in
    itself, and getting a rare name slightly wrong is better than saying «для Лёва».
    """
    if not name:
        return name
    if name.endswith(("а", "я")):
        stem = name[:-1]
        return stem + ("и" if name.endswith("я") or stem.endswith(("к", "г", "х", "ж", "ш", "щ", "ч")) else "ы")
    if name.endswith("й"):
        return name[:-1] + "я"
    if name.endswith("ь"):
        return name[:-1] + "я"
    return name + "а"


def ru_time(value: datetime) -> str:
    """Время события в часовом поясе семьи — в базе оно лежит в UTC."""
    local = to_local(value)
    return local.strftime("%H:%M") if local else ""


def ru_date(value: datetime) -> str:
    local = to_local(value)
    if not local:
        return ""
    return f"{local.day} {_MONTHS[local.month - 1]}"


def ru_datetime(value: datetime, now: datetime = None) -> str:
    local = to_local(value)
    if not local:
        return ""
    now = now or local_now()
    if local.date() == now.date():
        return f"Сегодня, {local.strftime('%H:%M')}"
    if (now.date() - local.date()).days == 1:
        return f"Вчера, {local.strftime('%H:%M')}"
    return f"{ru_date(value)}, {local.strftime('%H:%M')}"


def filesize(value: int) -> str:
    """«340 КБ» / «18,4 МБ» — снимки и видео отличаются на два порядка."""
    if not value:
        return ""
    if value < 1024 * 1024:
        return f"{round(value / 1024)} КБ"
    if value < 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024:.1f}".replace(".", ",") + " МБ"
    return f"{value / 1024 / 1024 / 1024:.1f}".replace(".", ",") + " ГБ"


def weekday_short(value: datetime) -> str:
    local = to_local(value)
    return _WEEKDAYS[local.weekday()][:2] if local else ""


templates.env.filters["plural"] = plural
templates.env.filters["counted"] = counted
templates.env.filters["genitive"] = genitive
templates.env.filters["ru_time"] = ru_time
templates.env.filters["ru_date"] = ru_date
templates.env.filters["ru_datetime"] = ru_datetime
templates.env.filters["filesize"] = filesize
templates.env.filters["weekday_short"] = weekday_short
