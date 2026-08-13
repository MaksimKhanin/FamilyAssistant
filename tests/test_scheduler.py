"""Планировщик: расписания, напоминания и готовность к работе в одиночку."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import inspect

from app import scheduler
from app.core.db import Base, engine
from app.core.events import AGENT_MESSAGE, bus
from app.core.models import ScheduledJob
from app.modules.memory import reminders as reminders_service


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


def test_the_regular_figure_of_a_board_arrives_in_the_existing_digest(db, head, catcher,
                                                                      monkeypatch):
    """Задача статистики не заводит своего расписания (тикет #31).

    Второй поток уведомлений семье не нужен: цифра едет той же утренней сводкой,
    что и всё остальное.
    """
    from app.modules.memory import knowledge, stats
    from tests.conftest import FakeLLM

    section = knowledge.create_section(db, head.id, "Малыш")
    board = knowledge.create_board(db, head.id, section.id, "Кормления")
    knowledge.add_event_type(db, head.id, board.id, "кормление", "мл")
    knowledge.add_entry(db, head.id, board.id, "02:50 170", llm=FakeLLM([
        {"events": [{"kind": "кормление", "value": 170, "unit": "мл", "confidence": "high"}]}]))
    stats.create_task(db, head.id, board.id, request="сколько малыш съел за сутки",
                      kind="кормление")
    monkeypatch.setattr(stats, "default_client",
                        FakeLLM([{"text": "За сутки малыш съел 170 мл."}]))

    db.add(ScheduledJob(user_id=head.id, kind="morning_digest", at_time="08:00", enabled=True))
    db.commit()
    scheduler.run_jobs(db, datetime(2026, 8, 9, 8, 0))

    assert catcher, "утренняя сводка не ушла"
    assert "За сутки малыш съел 170 мл." in catcher[-1]["text"]


# --- напоминания ----------------------------------------------------------

def test_a_due_reminder_reaches_its_owner_once(db, head, catcher):
    from app.modules.memory.models import Reminder

    reminders_service.add_reminder(db, head.id, "полить цветы",
                                   remind_at=datetime.utcnow() - timedelta(minutes=1))

    scheduler.run_reminders(db, datetime.utcnow())
    scheduler.run_reminders(db, datetime.utcnow())

    reminders = [m for m in catcher if "Напоминаю" in m["text"]]
    assert len(reminders) == 1
    assert "полить цветы" in reminders[0]["text"]
    assert reminders[0]["user_ids"] == [head.id]
    # Сработавшее помечено — второй раз оно уже не уйдёт.
    assert db.query(Reminder).one().reminded_at is not None


def test_a_future_reminder_waits(db, head, catcher):
    reminders_service.add_reminder(db, head.id, "позвонить врачу",
                                   remind_at=datetime.utcnow() + timedelta(hours=1))

    scheduler.run_reminders(db, datetime.utcnow())

    assert [m for m in catcher if "Напоминаю" in m["text"]] == []


