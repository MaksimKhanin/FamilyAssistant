"""Время: храним в UTC, показываем и считаем в часовом поясе семьи.

В базе всё в UTC — иначе перевод часов или переезд превращают историю в кашу.
Но человеку важно ровно обратное: «23:14» должно значить 23:14 у него дома, а
«сегодня» — его календарный день, а не гринвичский. Всё, что переводит одно в
другое, живёт здесь; напрямую `datetime.utcnow()` для показа и для границ суток
использовать не надо.

Часовой пояс задаётся переменной `TIMEZONE` (по умолчанию Europe/Moscow).
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional, Tuple

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("clock")


def _resolve_zone():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings.timezone)
    except Exception:
        logger.warning(f"Не знаю часового пояса «{settings.timezone}» — считаю время по UTC")
        return timezone.utc


LOCAL_ZONE = _resolve_zone()


def utc_now() -> datetime:
    """Наивный UTC — то, что лежит в базе."""
    return datetime.utcnow()


def to_local(value: Optional[datetime]) -> Optional[datetime]:
    """Наивный UTC из базы → наивное локальное время для показа и правил."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_ZONE).replace(tzinfo=None)


def to_utc(value: datetime) -> datetime:
    """Наивное локальное время → наивный UTC для записи в базу."""
    return value.replace(tzinfo=LOCAL_ZONE).astimezone(timezone.utc).replace(tzinfo=None)


def local_now() -> datetime:
    return to_local(utc_now())


def local_today() -> date:
    return local_now().date()


def local_date(value: Optional[datetime]) -> Optional[date]:
    """Календарный день события глазами семьи."""
    local = to_local(value)
    return local.date() if local else None


def day_bounds_utc(day: date) -> Tuple[datetime, datetime]:
    """Границы локальных суток в UTC — для запросов «что было сегодня»."""
    start = to_utc(datetime.combine(day, time.min))
    end = to_utc(datetime.combine(day + timedelta(days=1), time.min))
    return start, end


def days_ago_start_utc(days: int) -> datetime:
    """Начало локальных суток, которые были `days` дней назад."""
    start, _ = day_bounds_utc(local_today() - timedelta(days=days))
    return start
