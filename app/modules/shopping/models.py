"""Shopping tables — one list per family, scoped by family_id.

Вычеркнутое не удаляется сразу: в магазине список читают с телефона, и
вычеркнутая строка — это «уже в корзине», а не «никогда не существовало».
Убирает вычеркнутое ночная уборка планировщика (retention в service.py).
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.core.db import Base


class ShoppingItem(Base):
    __tablename__ = "shopping_items"

    id = Column(Integer, primary_key=True)
    family_id = Column(Integer, ForeignKey("families.id", ondelete="CASCADE"),
                       nullable=False, index=True)

    text = Column(String(255), nullable=False)
    #: Кто положил — SET NULL, а не CASCADE: список переживает автора строки,
    #: как записи на досках (ADR-0004).
    added_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    checked = Column(Boolean, nullable=False, default=False)
    checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
