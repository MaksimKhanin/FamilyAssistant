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


def test_a_job_fires_at_its_local_time(db, member, catcher):
    db.add(ScheduledJob(user_id=member.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert catcher, "вечерний итог не ушёл"
    assert catcher[-1]["user_ids"] == [member.id]
    assert "Вечерний итог" in catcher[-1]["text"]


def test_a_job_does_not_fire_at_another_minute(db, member, catcher):
    db.add(ScheduledJob(user_id=member.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 20, 59))

    assert catcher == []


def test_a_disabled_job_stays_silent(db, member, catcher):
    db.add(ScheduledJob(user_id=member.id, kind="evening_summary", at_time="21:00", enabled=False))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert catcher == []


def test_a_job_does_not_fire_twice_in_the_same_minute(db, member, catcher):
    db.add(ScheduledJob(user_id=member.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))
    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert len(catcher) == 1


def test_the_regular_figure_of_a_board_arrives_in_the_existing_digest(db, member, catcher,
                                                                      monkeypatch):
    """Задача статистики не заводит своего расписания (тикет #31).

    Второй поток уведомлений семье не нужен: цифра едет той же утренней сводкой,
    что и всё остальное.
    """
    from app.modules.memory import knowledge, stats
    from tests.conftest import FakeLLM

    section = knowledge.create_section(db, member.id, "Малыш")
    board = knowledge.create_board(db, member.id, section.id, "Кормления")
    knowledge.add_event_type(db, member.id, board.id, "кормление", "мл")
    knowledge.add_entry(db, member.id, board.id, "02:50 170", llm=FakeLLM([
        {"events": [{"kind": "кормление", "value": 170, "unit": "мл", "confidence": "high"}]}]))
    stats.create_task(db, member.id, board.id, request="сколько малыш съел за сутки",
                      kind="кормление")
    monkeypatch.setattr(stats, "default_client",
                        FakeLLM([{"text": "За сутки малыш съел 170 мл."}]))

    # Момент прогона — «сейчас», а не зашитая дата: запись и её событие только
    # что созданы, и окно суточной статистики должно их накрывать в любой день.
    now = datetime.utcnow()
    db.add(ScheduledJob(user_id=member.id, kind="morning_digest",
                        at_time=now.strftime("%H:%M"), enabled=True))
    db.commit()
    scheduler.run_jobs(db, now)

    assert catcher, "утренняя сводка не ушла"
    assert "За сутки малыш съел 170 мл." in catcher[-1]["text"]


# --- напоминания ----------------------------------------------------------

def test_a_due_reminder_reaches_its_owner_once(db, member, catcher):
    from app.modules.memory.models import Reminder

    reminders_service.add_reminder(db, member.id, "полить цветы",
                                   remind_at=datetime.utcnow() - timedelta(minutes=1))

    scheduler.run_reminders(db, datetime.utcnow())
    scheduler.run_reminders(db, datetime.utcnow())

    reminders = [m for m in catcher if "Напоминаю" in m["text"]]
    assert len(reminders) == 1
    assert "полить цветы" in reminders[0]["text"]
    assert reminders[0]["user_ids"] == [member.id]
    # Сработавшее помечено — второй раз оно уже не уйдёт.
    assert db.query(Reminder).one().reminded_at is not None


def test_a_future_reminder_waits(db, member, catcher):
    reminders_service.add_reminder(db, member.id, "позвонить врачу",
                                   remind_at=datetime.utcnow() + timedelta(hours=1))

    scheduler.run_reminders(db, datetime.utcnow())

    assert [m for m in catcher if "Напоминаю" in m["text"]] == []



# --- голос персоны в сводках и напоминаниях (тикет #72) ---------------------

def test_a_digest_can_speak_in_character(db, member, catcher, monkeypatch):
    """Сводка уходит голосом персоны; факты при этом собраны кодом."""
    from app.agent import voice

    heard = {}

    def fake_speak(subject, hint, fallback="", llm=None):
        heard["hint"] = hint
        heard["fallback"] = fallback
        return "Доброе утро, солнышко! Дома всё спокойно."

    monkeypatch.setattr(voice, "speak", fake_speak)
    db.add(ScheduledJob(user_id=member.id, kind="morning_digest", at_time="08:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 8, 0))

    assert catcher[-1]["text"] == "Доброе утро, солнышко! Дома всё спокойно."
    # запасной текст — прежняя каноническая сводка
    assert heard["fallback"].startswith("Доброе утро.")
    # факты доехали в просьбу дословно
    assert "Факты:" in heard["hint"]


def test_a_digest_without_the_model_keeps_the_canonical_form(db, member, catcher):
    """Модель недоступна (в тестах LLM_BASE_URL ведёт в никуда) — прежний формат."""
    db.add(ScheduledJob(user_id=member.id, kind="evening_summary", at_time="21:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 21, 0))

    assert catcher[-1]["text"].startswith("Вечерний итог.")


def test_a_reminder_can_speak_in_character(db, member, catcher, monkeypatch):
    from datetime import timedelta as _td
    from app.agent import voice

    monkeypatch.setattr(voice, "speak",
                        lambda subject, hint, fallback="", llm=None:
                        "Милая, пора выпить лекарство — ты просила напомнить.")
    reminders_service.add_reminder(db, member.id, "выпить лекарство",
                                   remind_at=datetime.utcnow() - _td(minutes=1))

    scheduler.run_reminders(db, datetime.utcnow())

    assert catcher[-1]["text"] == "Милая, пора выпить лекарство — ты просила напомнить."


def test_a_reminder_without_the_model_keeps_the_canonical_form(db, member, catcher):
    from datetime import timedelta as _td

    reminders_service.add_reminder(db, member.id, "полить цветы",
                                   remind_at=datetime.utcnow() - _td(minutes=1))

    scheduler.run_reminders(db, datetime.utcnow())

    assert catcher[-1]["text"] == "Напоминаю: полить цветы"
