"""Notes the assistant keeps about a person — personal, scoped by user_id."""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.core.db import Base

#: Note kinds, matching the coloured badges in the design.
KIND_PREF = "pref"      # предпочтение
KIND_HEALTH = "health"  # здоровье
KIND_TASK = "task"      # напоминание
KIND_FACT = "fact"      # наблюдение

KIND_LABELS = {
    KIND_PREF: "предпочтение",
    KIND_HEALTH: "здоровье",
    KIND_TASK: "напоминание",
    KIND_FACT: "наблюдение",
}


class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    text = Column(Text, nullable=False)
    kind = Column(String(16), nullable=False, default=KIND_FACT)
    source = Column(String(64), nullable=False, default="из разговора")
    pinned = Column(Boolean, nullable=False, default=False)

    #: Свободная формулировка («в пятницу утром») плюс точное время, если его удалось понять.
    when_text = Column(String(128), nullable=True)
    remind_at = Column(DateTime, nullable=True, index=True)
    reminded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
