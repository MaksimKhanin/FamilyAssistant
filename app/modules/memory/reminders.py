"""Разовые напоминания — отдельная способность ассистента, вне знаний (спека #19).

Единственные ворота в таблицу — `add_reminder` после `parse_remind_at` и
`validate_remind_at`: напоминание с расплывчатым или бессмысленным временем не
создаётся вовсе, ассистент переспрашивает. Сработавшее остаётся в списке
помеченным и убирается ретеншеном.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.clock import to_utc, utc_now
from app.modules.memory.models import Reminder

#: Дальше этого горизонта время не принимается: «через три года» — почти
#: наверняка ошибка разбора, а не план человека.
MAX_AHEAD = timedelta(days=365)

#: Сколько сработавшее напоминание висит в списке помеченным, прежде чем
#: ночная уборка планировщика его уберёт.
FIRED_RETENTION_DAYS = 7


#: Дата обязана идти вместе со временем: «2026-08-15» без часов — это не момент,
#: а день, и полуночное напоминание из него выдумывать нельзя.
_DATE_WITH_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse_remind_at(raw: str) -> Optional[datetime]:
    """Абсолютное локальное время от модели → наивный UTC для базы.

    Понимает только ISO-подобное «2026-08-15 09:00». Всё остальное — не время:
    расплывчатое «в пятницу утром», бессмысленное «25:00» и голая дата без
    часов отсекаются здесь.
    """
    if not raw or not _DATE_WITH_TIME.match(raw.strip()):
        return None
    try:
        value = datetime.fromisoformat(raw.strip().replace(" ", "T"))
    except ValueError:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(value)


def validate_remind_at(remind_at: datetime, now: datetime = None) -> Optional[str]:
    """Вменяемость времени: в будущем и не дальше года. Возвращает причину отказа."""
    now = now or utc_now()
    if remind_at <= now:
        return "Это время уже прошло."
    if remind_at - now > MAX_AHEAD:
        return "Это дальше, чем через год."
    return None


#: Какие повторения бывают. Слова человеческие, потому что уезжают на экран.
RECURRENCE_WORDS = {"daily": "каждый день", "weekly": "каждую неделю", "monthly": "каждый месяц"}


def parse_recurrence(raw: str) -> Optional[str]:
    """'daily' | 'weekly' | 'monthly' — или None для разового (и для мусора)."""
    value = (raw or "").strip().lower()
    return value if value in RECURRENCE_WORDS else None


def next_occurrence(remind_at: datetime, recurrence: str, now: datetime = None) -> datetime:
    """Следующий момент повторяющегося напоминания — строго в будущем.

    Шаг идёт от собственного `remind_at`, а не от момента срабатывания: так
    «каждый вторник в 9» остаётся вторником в 9, даже если планировщик догнал
    напоминание с опозданием. Месячный шаг держит число, поджимая его к длине
    месяца (31-е → 28-е февраля), — так «каждое 31-е» не пропадает в коротких
    месяцах.
    """
    now = now or utc_now()
    value = remind_at
    while value <= now:
        if recurrence == "daily":
            value = value + timedelta(days=1)
        elif recurrence == "weekly":
            value = value + timedelta(days=7)
        else:  # monthly
            year, month = value.year, value.month
            if month == 12:
                year, month = year + 1, 1
            else:
                month += 1
            day = min(remind_at.day, _month_days(year, month))
            value = value.replace(year=year, month=month, day=day)
    return value


def _month_days(year: int, month: int) -> int:
    import calendar
    return calendar.monthrange(year, month)[1]


def add_reminder(db: Session, user_id: int, text: str, remind_at: datetime,
                 recurrence: str = None) -> Reminder:
    reminder = Reminder(user_id=user_id, text=text.strip(), remind_at=remind_at,
                        recurrence=parse_recurrence(recurrence))
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    return reminder


def cancel_reminder(db: Session, user_id: int, reminder_id: int) -> bool:
    """Снять ещё не сработавшее напоминание — опечатался во времени, передумал,
    поставил дважды. Сработавшее не трогает: то уже история, а не план, и его
    убирает ретеншен."""
    reminder = (
        db.query(Reminder)
        .filter(Reminder.id == reminder_id, Reminder.user_id == user_id,
               Reminder.reminded_at.is_(None))
        .one_or_none()
    )
    if reminder is None:
        return False
    db.delete(reminder)
    db.commit()
    return True


def list_active(db: Session, user_id: int) -> List[Reminder]:
    """Ещё не сработавшие, ближайшие первыми."""
    return (
        db.query(Reminder)
        .filter(Reminder.user_id == user_id, Reminder.reminded_at.is_(None))
        .order_by(Reminder.remind_at)
        .all()
    )


def list_fired(db: Session, user_id: int) -> List[Reminder]:
    """Недавно сработавшие — старше их ретеншен уже убрал."""
    return (
        db.query(Reminder)
        .filter(Reminder.user_id == user_id, Reminder.reminded_at.isnot(None))
        .order_by(Reminder.reminded_at.desc())
        .all()
    )


def due_reminders(db: Session, now: datetime = None) -> List[Reminder]:
    now = now or utc_now()
    return (
        db.query(Reminder)
        .filter(Reminder.reminded_at.is_(None), Reminder.remind_at <= now)
        .order_by(Reminder.remind_at)
        .all()
    )


def purge_fired(db: Session, now: datetime = None) -> int:
    """Убрать давно сработавшие. Возвращает число удалённых."""
    cutoff = (now or utc_now()) - timedelta(days=FIRED_RETENTION_DAYS)
    removed = (
        db.query(Reminder)
        .filter(Reminder.reminded_at.isnot(None), Reminder.reminded_at < cutoff)
        .delete()
    )
    if removed:
        db.commit()
    return removed
