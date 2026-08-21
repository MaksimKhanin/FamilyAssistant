"""Движок идей: предлагает, не делает и умеет молчать (тикет #83)."""
from datetime import datetime, timedelta

from app.core.models import ActionLog
from app.modules.memory import knowledge
from app.modules.relationship import ideas
from tests.conftest import FakeLLM


def _busy_week(db, member, tool="log_activity", times=6):
    for _ in range(times):
        db.add(ActionLog(user_id=member.id, tool=tool, summary="шаги"))
    db.commit()


def test_ideas_land_on_their_board(db, member):
    _busy_week(db, member)

    written = ideas.run_ideas(db, member, llm=FakeLLM([{
        "ideas": ["Вы каждый вечер записываете шаги руками — поставить табло?"]
    }]))

    assert written == ["Вы каждый вечер записываете шаги руками — поставить табло?"]
    board = knowledge.ideas_board(db, member.id, create=False)
    entries = knowledge.list_entries(db, member.id, board.id)
    assert [e.text for e in entries] == written
    assert entries[0].by_assistant


def test_no_more_than_two_ideas_per_week(db, member):
    _busy_week(db, member)

    written = ideas.run_ideas(db, member, llm=FakeLLM([{
        "ideas": ["раз", "два", "три", "четыре"]
    }]))

    assert written == ["раз", "два"]


def test_a_quiet_user_is_left_alone(db, member):
    """Меньше MIN_ACTIONS действий за неделю — ни идей, ни вызова модели."""
    llm = FakeLLM([])          # любой вызов уронит тест
    assert ideas.run_ideas(db, member, llm=llm) == []


def test_a_rule_against_ideas_mutes_the_engine(db, member):
    _busy_week(db, member)
    knowledge.add_rule(db, member.id, "не предлагай мне идей")

    llm = FakeLLM([])
    assert ideas.run_ideas(db, member, llm=llm) == []


def test_an_empty_run_still_postpones_the_next_one(db, member):
    _busy_week(db, member)

    assert ideas.due(db, member.id)
    ideas.run_ideas(db, member, llm=FakeLLM([{"ideas": []}]))

    assert not ideas.due(db, member.id)
    board = knowledge.ideas_board(db, member.id, create=False)
    assert knowledge.list_entries(db, member.id, board.id) == []


def test_fresh_ideas_reach_the_weekly_digest(db, member, monkeypatch):
    from app import scheduler
    from app.agent import voice
    from app.core.events import AGENT_MESSAGE, bus
    from app.core.models import ScheduledJob

    _busy_week(db, member)
    ideas.run_ideas(db, member, llm=FakeLLM([{"ideas": ["Поставить табло шагов?"]}]))

    received = []
    bus.subscribe(AGENT_MESSAGE, received.append)
    monkeypatch.setattr(voice, "speak", lambda subject, hint, fallback="", llm=None: fallback)
    db.add(ScheduledJob(user_id=member.id, kind="weekly_review", at_time="19:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 19, 0))

    assert any("Поставить табло шагов?" in m["text"] for m in received)
