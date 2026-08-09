"""Note storage and retrieval.

Search is deliberately plain substring matching over a household's worth of notes
(tens, not millions). The RAG index on the «Модель и знания» screen is a separate,
later concern — see docs/architecture.md.
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.modules.memory.models import KIND_FACT, KIND_LABELS, Note


def add_note(db: Session, user_id: int, text: str, kind: str = KIND_FACT,
             source: str = "из разговора", when_text: str = None,
             remind_at: datetime = None) -> Note:
    note = Note(
        user_id=user_id,
        text=text.strip(),
        kind=kind if kind in KIND_LABELS else KIND_FACT,
        source=source,
        when_text=when_text,
        remind_at=remind_at,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def list_notes(db: Session, user_id: int, kind: str = None, limit: int = 100) -> List[Note]:
    query = db.query(Note).filter(Note.user_id == user_id)
    if kind:
        query = query.filter(Note.kind == kind)
    return (
        query.order_by(Note.pinned.desc(), Note.created_at.desc())
        .limit(limit)
        .all()
    )


def search_notes(db: Session, user_id: int, query: str = None, kind: str = None,
                 limit: int = 8) -> List[Note]:
    rows = db.query(Note).filter(Note.user_id == user_id)
    if kind:
        rows = rows.filter(Note.kind == kind)
    if query:
        rows = rows.filter(Note.text.ilike(f"%{query.strip()}%"))
    return rows.order_by(Note.pinned.desc(), Note.created_at.desc()).limit(limit).all()


def get_note(db: Session, user_id: int, note_id: int) -> Optional[Note]:
    note = db.get(Note, note_id)
    return note if note is not None and note.user_id == user_id else None


def toggle_pin(db: Session, user_id: int, note_id: int) -> Optional[Note]:
    note = get_note(db, user_id, note_id)
    if note is None:
        return None
    note.pinned = not note.pinned
    db.commit()
    return note


def forget(db: Session, user_id: int, note_id: int) -> bool:
    note = get_note(db, user_id, note_id)
    if note is None:
        return False
    db.delete(note)
    db.commit()
    return True


def due_reminders(db: Session, now: datetime = None) -> List[Note]:
    now = now or datetime.utcnow()
    return (
        db.query(Note)
        .filter(Note.remind_at.isnot(None), Note.remind_at <= now, Note.reminded_at.is_(None))
        .order_by(Note.remind_at)
        .all()
    )


def counters(db: Session, user_id: int) -> dict:
    notes = db.query(Note).filter(Note.user_id == user_id).all()
    return {
        "total": len(notes),
        "waiting": sum(1 for n in notes if n.remind_at and not n.reminded_at),
        "pinned": sum(1 for n in notes if n.pinned),
    }
