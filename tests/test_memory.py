"""Старые заметки: сервис жив до переезда на доски (#33), экран пока на месте."""
from datetime import datetime, timedelta

from app.modules.memory import service


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
