"""Knowledge a person keeps with the assistant — personal, scoped by user_id.

Two generations side by side: the old flat notes (below) and the knowledge
schema replacing them — sections → boards → entries, plus per-person shares
(spec #19). Notes stay until the data migration moves them onto boards.
"""
from datetime import datetime

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint)

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


# --- знания: разделы → доски → записи (спека #19) ---

#: Права доступа к чужой доске (см. board_shares.right).
RIGHT_VIEW = "view"    # просмотр
RIGHT_EDIT = "edit"    # редактирование: свои записи; чужие правит только владелец


class Section(Base):
    """Раздел — личная рубрика знаний. Чужих разделов не видит никто."""
    __tablename__ = "sections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(128), nullable=False)
    pinned = Column(Boolean, nullable=False, default=False)
    #: Денормализованное время последней записи на досках раздела — по нему
    #: сортируется полоса разделов (закреплённые, затем по свежести).
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Board(Base):
    """Доска — лента записей с инструкцией ассистенту.

    Владелец не дублируется: вычисляется через раздел (`section.user_id`), чтобы
    перенос доски между разделами не мог разъехаться с правами.
    """
    __tablename__ = "boards"

    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(128), nullable=False)
    #: Как ассистенту читать и вести содержимое: «19.50 170 — время и миллилитры».
    instruction = Column(Text, nullable=True)
    #: «Всем» — живое правило, а не снимок: новый человек в семье получает
    #: такую доску сам, без повторного действия владельца (спека #19).
    share_all = Column(Boolean, nullable=False, default=False)
    share_all_right = Column(String(8), nullable=True)
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardEntry(Base):
    """Запись в ленте доски. Принадлежит документу, а не автору (ADR-0004).

    Три вида авторства различаются парой полей, потому что `author_id = NULL`
    занят ушедшим участником: (id, false) — человек, (NULL, true) — ассистент,
    (NULL, false) — «бывший участник».
    """
    __tablename__ = "board_entries"

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    by_assistant = Column(Boolean, nullable=False, default=False)

    text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    #: Правки не тихие: у поправленной записи в ленте видна пометка «изменено».
    edited_at = Column(DateTime, nullable=True)


class BoardEventType(Base):
    """Словарь величин одной доски: «кормление» в мл, «прогулка» в минутах.

    Тип берётся отсюда, а не из головы модели: иначе «кормление», «еда» и
    «молоко» завелись бы на одной доске вперемешку. Съеденное и потраченное —
    два разных типа, а не число со знаком.
    """
    __tablename__ = "board_event_types"
    __table_args__ = (UniqueConstraint("board_id", "name", name="uq_board_event_type"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(64), nullable=False)
    unit = Column(String(16), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardEvent(Base):
    """Величина, извлечённая из записи: тип, время, число и единица.

    Живёт ровно столько, сколько живёт её запись: правка записи переразбирает
    события, удаление — уносит. Разбор происходит один раз, при написании, а не
    при сборке сводки: цифра за прошлый вторник не должна меняться оттого, что
    сегодня модель прочла лог иначе (ADR-0002).
    """
    __tablename__ = "board_events"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("board_entries.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    #: Доска дублируется рядом с записью: статистику считают по доске за период,
    #: и ходить за этим в записи — лишний join на каждой сводке.
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String(64), nullable=False)
    at = Column(DateTime, nullable=False, index=True)
    value = Column(Float, nullable=False)
    unit = Column(String(16), nullable=True)
    #: low — в сумму не идёт, пока человек не уточнил (спека #19).
    confidence = Column(String(8), nullable=False, default="low")
    #: Фрагмент записи, из которого взята величина, — им и спрашивают человека.
    raw = Column(String(255), nullable=True)


class BoardShare(Base):
    """Поимённый доступ к доске: просмотр или редактирование."""
    __tablename__ = "board_shares"
    __table_args__ = (UniqueConstraint("board_id", "user_id", name="uq_board_share"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    right = Column(String(8), nullable=False, default=RIGHT_VIEW)


class Reminder(Base):
    """Разовое напоминание — отдельная способность вне знаний (спека #19).

    Живёт только с валидным абсолютным временем: без времени напоминание не
    создаётся, ассистент переспрашивает. Сработавшее остаётся помеченным
    (`reminded_at`) и убирается ретеншеном — руками его не закрывают.
    """
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    text = Column(Text, nullable=False)
    remind_at = Column(DateTime, nullable=False, index=True)
    reminded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


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
