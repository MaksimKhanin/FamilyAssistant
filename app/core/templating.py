"""Single Jinja2 environment shared by the core and every module.

Templates live under app/templates/, each module keeping its own subdirectory
(templates/nutrition/..., templates/security/...) and extending the shared base.
"""
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


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
    return value.strftime("%H:%M") if value else ""


def ru_date(value: datetime) -> str:
    if not value:
        return ""
    return f"{value.day} {_MONTHS[value.month - 1]}"


def ru_datetime(value: datetime, now: datetime = None) -> str:
    if not value:
        return ""
    now = now or datetime.utcnow()
    if value.date() == now.date():
        return f"Сегодня, {value.strftime('%H:%M')}"
    if (now.date() - value.date()).days == 1:
        return f"Вчера, {value.strftime('%H:%M')}"
    return f"{ru_date(value)}, {value.strftime('%H:%M')}"


def weekday_short(value: datetime) -> str:
    return _WEEKDAYS[value.weekday()][:2] if value else ""


templates.env.filters["plural"] = plural
templates.env.filters["counted"] = counted
templates.env.filters["genitive"] = genitive
templates.env.filters["ru_time"] = ru_time
templates.env.filters["ru_date"] = ru_date
templates.env.filters["ru_datetime"] = ru_datetime
templates.env.filters["weekday_short"] = weekday_short
