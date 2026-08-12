"""Планировщик: расписания, напоминания и готовность к работе в одиночку."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect

from app import scheduler
from app.core.db import Base, engine
from app.core.events import AGENT_MESSAGE, bus
from app.core.models import ScheduledJob
from app.modules.memory import service as memory


def test_scheduler_creates_the_schema_it_needs(db):
    """Веб и планировщик стартуют одновременно.

    Раньше схему создавал только веб, и планировщик, выиграв гонку, раз в минуту
    спрашивал несуществующую таблицу — база сыпала «relation does not exist».
    """
    Base.metadata.drop_all(bind=engine)
    assert "scheduled_jobs" not in inspect(engine).get_table_names()

    scheduler.prepare()

    assert "scheduled_jobs" in inspect(engine).get_table_names()


def test_a_tick_on_a_fresh_database_does_not_explode(db):
    Base.metadata.drop_all(bind=engine)
    scheduler.prepare()

    scheduler.tick()          # ни одной задачи и ни одного напоминания — просто тихо


# --- расписания -----------------------------------------------------------

@pytest.fixture
def catcher():
    received = []
    bus.subscribe(AGENT_MESSAGE, received.append)
    return received


def test_a_job_fires_at_its_local_time(db, head, catcher):
    db.add(ScheduledJob(user_id=head.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert catcher, "вечерний итог не ушёл"
    assert catcher[-1]["user_ids"] == [head.id]
    assert "Вечерний итог" in catcher[-1]["text"]


def test_a_job_does_not_fire_at_another_minute(db, head, catcher):
    db.add(ScheduledJob(user_id=head.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 20, 59))

    assert catcher == []


def test_a_disabled_job_stays_silent(db, head, catcher):
    db.add(ScheduledJob(user_id=head.id, kind="evening_summary", at_time="21:00", enabled=False))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert catcher == []


def test_a_job_does_not_fire_twice_in_the_same_minute(db, head, catcher):
    db.add(ScheduledJob(user_id=head.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))
    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert len(catcher) == 1


# --- напоминания ----------------------------------------------------------

def test_a_due_reminder_reaches_its_owner_once(db, head, catcher):
    memory.add_note(db, head.id, "полить цветы",
                    remind_at=datetime.utcnow() - timedelta(minutes=1))

    scheduler.run_reminders(db, datetime.utcnow())
    scheduler.run_reminders(db, datetime.utcnow())

    reminders = [m for m in catcher if "Напоминаю" in m["text"]]
    assert len(reminders) == 1
    assert "полить цветы" in reminders[0]["text"]


def test_a_future_reminder_waits(db, head, catcher):
    memory.add_note(db, head.id, "позвонить врачу",
                    remind_at=datetime.utcnow() + timedelta(hours=1))

    scheduler.run_reminders(db, datetime.utcnow())

    assert [m for m in catcher if "Напоминаю" in m["text"]] == []


def test_a_due_reminder_from_the_reminders_table_reaches_its_owner_once(db, head, catcher):
    """Новая таблица напоминаний доставляется той же механикой, что и заметки."""
    from app.modules.memory.models import Reminder

    db.add(Reminder(user_id=head.id, text="забрать посылку",
                    remind_at=datetime.utcnow() - timedelta(minutes=1)))
    db.commit()

    scheduler.run_reminders(db, datetime.utcnow())
    scheduler.run_reminders(db, datetime.utcnow())

    reminders = [m for m in catcher if "забрать посылку" in m["text"]]
    assert len(reminders) == 1
    assert reminders[0]["user_ids"] == [head.id]

    fired = db.query(Reminder).one()
    assert fired.reminded_at is not None
