"""Memory: notes are personal, and reminders resurface on time."""
from datetime import datetime, timedelta

from app.agent.registry import ToolContext
from app.modules.memory import service
from app.modules.memory.models import KIND_PREF, KIND_TASK
from app.modules.memory.tools import recall_notes, remember


def ctx(db, user) -> ToolContext:
    return ToolContext(db=db, actor=user, subject=user)


def test_remember_stores_a_note_and_returns_a_card(db, head):
    result = remember(ctx(db, head), text="Соня не ест грибы", kind=KIND_PREF)

    assert result.ok
    note = service.list_notes(db, head.id)[0]
    assert note.text == "Соня не ест грибы"
    assert result.card["type"] == "memory"
    assert result.card["kind_label"] == "предпочтение"


def test_remember_no_longer_offers_the_reminder_path(db):
    """Напоминания заводит только set_reminder — у remember этой дороги больше нет.

    Иначе у модели оставался бы второй, невалидируемый путь: заметка вида «task»
    с расплывчатым сроком, которая молча не срабатывает (спека #19).
    """
    from app.agent import registry

    spec = registry.get("remember")
    assert "when" not in spec.parameters["properties"]
    assert KIND_TASK not in spec.parameters["properties"]["kind"]["enum"]


def test_recall_only_reaches_its_owners_notes(db, head, member):
    remember(ctx(db, head), text="Марина любит зелёный чай", kind=KIND_PREF)

    assert "зелёный чай" in recall_notes(ctx(db, head), query="чай").summary
    assert recall_notes(ctx(db, member), query="чай").data["notes"] == []


def test_due_reminders_are_picked_up_once(db, head):
    service.add_note(db, head.id, "полить цветы", remind_at=datetime.utcnow() - timedelta(minutes=5))

    due = service.due_reminders(db)
    assert len(due) == 1

    due[0].reminded_at = datetime.utcnow()
    db.commit()
    assert service.due_reminders(db) == []


def test_pinned_notes_come_first(db, head):
    first = service.add_note(db, head.id, "старая заметка")
    service.add_note(db, head.id, "новая заметка")

    service.toggle_pin(db, head.id, first.id)
    assert service.list_notes(db, head.id)[0].id == first.id


def test_counters_feed_the_sidebar_card(db, head):
    service.add_note(db, head.id, "раз")
    service.add_note(db, head.id, "два", remind_at=datetime.utcnow() + timedelta(days=1))

    assert service.counters(db, head.id) == {"total": 2, "waiting": 1, "pinned": 0}
