"""Напоминания: отдельная способность — валидное абсолютное время или ничего.

Напоминание без времени не создаётся: ассистент переспрашивает, а не заводит
молчаливую пустышку. Сработавшее остаётся в списке помеченным и убирается
ретеншеном — руками его не закрывают.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMResponse, ToolCall
from app.agent.registry import ToolContext
from app.agent.runtime import Agent
from app.core.db import get_db
from app.main import app
from app.modules.memory import reminders as service
from app.modules.memory.models import Reminder
from app.modules.memory.tools import set_reminder
from tests.conftest import FakeLLM


def ctx(db, user) -> ToolContext:
    return ToolContext(db=db, actor=user, subject=user)


def at(delta: timedelta) -> str:
    """Абсолютное время «как от модели»: ISO без секунд. Тесты идут в UTC."""
    return (datetime.utcnow() + delta).strftime("%Y-%m-%dT%H:%M")


# --- инструмент: абсолютное время или отказ --------------------------------

def test_a_valid_absolute_time_creates_a_reminder(db, head):
    result = set_reminder(ctx(db, head), text="позвонить врачу", at=at(timedelta(days=1)))

    assert result.ok
    reminder = db.query(Reminder).one()
    assert reminder.user_id == head.id
    assert reminder.text == "позвонить врачу"
    assert reminder.remind_at > datetime.utcnow()
    assert reminder.reminded_at is None


def test_unparseable_time_creates_nothing_and_asks_to_clarify(db, head):
    result = set_reminder(ctx(db, head), text="про школу", at="в пятницу утром")

    assert not result.ok
    assert "переспроси" in result.summary.lower()
    assert db.query(Reminder).count() == 0


def test_nonsense_clock_time_is_rejected(db, head):
    result = set_reminder(ctx(db, head), text="про школу", at="2026-08-13T25:00")

    assert not result.ok
    assert db.query(Reminder).count() == 0


def test_a_bare_date_without_a_time_is_rejected(db, head):
    """«15 августа» без часов — это день, а не момент: полночь не выдумываем."""
    result = set_reminder(ctx(db, head), text="про школу",
                          at=at(timedelta(days=1))[:10])

    assert not result.ok
    assert db.query(Reminder).count() == 0


def test_past_time_is_rejected(db, head):
    result = set_reminder(ctx(db, head), text="вчерашнее", at=at(timedelta(hours=-1)))

    assert not result.ok
    assert db.query(Reminder).count() == 0


def test_time_beyond_a_year_is_rejected(db, head):
    result = set_reminder(ctx(db, head), text="через три года", at=at(timedelta(days=400)))

    assert not result.ok
    assert db.query(Reminder).count() == 0


def test_the_assistant_reasks_instead_of_creating_a_timeless_reminder(db, head):
    """Модель не назвала времени — инструмент отказал, ассистент переспросил."""
    head.autonomy = 3
    db.commit()

    llm = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="set_reminder",
                                         arguments={"text": "позвонить врачу"})]),
        LLMResponse(content="Когда напомнить?"),
    ])
    reply = Agent(llm).respond(db, head, "напомни позвонить врачу")

    assert reply.text == "Когда напомнить?"
    assert reply.traces[0].status == "failed"
    assert db.query(Reminder).count() == 0


# --- ретеншен сработавших ---------------------------------------------------

def test_purge_removes_only_long_fired_reminders(db, head):
    now = datetime.utcnow()
    keep_active = Reminder(user_id=head.id, text="живое", remind_at=now + timedelta(days=1))
    keep_fresh = Reminder(user_id=head.id, text="недавнее",
                          remind_at=now - timedelta(days=1),
                          reminded_at=now - timedelta(days=1))
    drop_stale = Reminder(user_id=head.id, text="давнее",
                          remind_at=now - timedelta(days=10),
                          reminded_at=now - timedelta(days=service.FIRED_RETENTION_DAYS + 1))
    db.add_all([keep_active, keep_fresh, drop_stale])
    db.commit()

    removed = service.purge_fired(db, now=now)

    assert removed == 1
    assert {r.text for r in db.query(Reminder)} == {"живое", "недавнее"}


# --- экран ------------------------------------------------------------------

@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_head(client, head):
    client.post("/login", data={"username": head.username, "password": "pw"},
                follow_redirects=False)
    return client


def test_the_screen_shows_active_and_recently_fired(db, head, as_head):
    now = datetime.utcnow()
    db.add_all([
        Reminder(user_id=head.id, text="полить цветы", remind_at=now + timedelta(hours=2)),
        Reminder(user_id=head.id, text="про врача", remind_at=now - timedelta(hours=2),
                 reminded_at=now - timedelta(hours=2)),
    ])
    db.commit()

    page = as_head.get("/reminders")

    assert page.status_code == 200
    assert "полить цветы" in page.text
    assert "про врача" in page.text
    assert "сработало" in page.text.lower()


def test_reminders_is_its_own_nav_item(db, head, as_head):
    page = as_head.get("/")

    assert 'href="/reminders"' in page.text
