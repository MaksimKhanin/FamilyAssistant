"""Core ORM models: the family, its people, and everything the agent layer needs
to decide what it may do on whose behalf.

Module-specific tables live in the modules themselves (app/modules/<name>/models.py)
and always carry `user_id` (personal data) or `family_id` (shared data).
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.db import Base

# --- small vocabularies, kept as plain strings so the DB stays easy to inspect ---

ROLE_HEAD = "head"        # глава семьи — включает модули, видит админ-раздел
ROLE_MEMBER = "member"

#: How a single tool may be used on behalf of a user.
MODE_AUTO = "auto"        # агент делает сам
MODE_ASK = "ask"          # готовит действие и спрашивает подтверждение
MODE_OFF = "off"          # инструмент недоступен

#: Autonomy slider positions (screen «Агент и инструменты»). Used as the default
#: tool mode when a user has no explicit per-tool override.
AUTONOMY_LEVELS = {
    0: "Всё спрашивает",
    1: "Спрашивает про важное",
    2: "Сам делает рутину",
    3: "Максимально самостоятельно",
}

#: Connector permission levels (screen «Коннекторы»).
CONN_OFF = "off"
CONN_READ = "read"
CONN_CONFIRM = "write"
CONN_ACT = "act"


class Family(Base):
    __tablename__ = "families"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, default="Семья")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    members = relationship("User", back_populates="family", cascade="all, delete-orphan")


class User(Base):
    """A person in the family. One row per Telegram account / web login."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    family_id = Column(Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True)

    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=True)
    display_name = Column(String(64), nullable=False)
    relation = Column(String(32), nullable=True)          # «мама», «сын», ... — только для тёплого тона UI
    role = Column(String(16), nullable=False, default=ROLE_MEMBER)

    telegram_id = Column(String(32), unique=True, nullable=True, index=True)
    link_code = Column(String(16), nullable=True, index=True)   # одноразовый код привязки Telegram

    avatar_slot = Column(Integer, nullable=False, default=0)    # индекс палитры аватара 0..4
    autonomy = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    family = relationship("Family", back_populates="members")

    @property
    def is_head(self) -> bool:
        return self.role == ROLE_HEAD


class ModuleAccess(Base):
    """The whole «ролевая модель» of the MVP: one boolean per user per module."""
    __tablename__ = "module_access"
    __table_args__ = (UniqueConstraint("user_id", "module", name="uq_module_access"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    module = Column(String(32), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)


class ToolPolicy(Base):
    """Per-user override of a tool's mode; absent row means «follow the autonomy slider»."""
    __tablename__ = "tool_policies"
    __table_args__ = (UniqueConstraint("user_id", "tool", name="uq_tool_policy"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tool = Column(String(64), nullable=False)
    mode = Column(String(8), nullable=False, default=MODE_ASK)


class PendingAction(Base):
    """A tool call the agent prepared but is not allowed to run without a human «да»."""
    __tablename__ = "pending_actions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tool = Column(String(64), nullable=False)
    arguments_json = Column(Text, nullable=False, default="{}")
    status = Column(String(16), nullable=False, default="pending")   # pending|approved|rejected|expired
    #: Вложение, которое не влезает в JSON-аргументы (фото блюда) — ждёт на диске
    #: вместе с действием и подхватывается при подтверждении.
    attachment_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)
    result_summary = Column(Text, nullable=True)


class ActionLog(Base):
    """«Что агент делал сегодня» — every tool invocation, however it was triggered."""
    __tablename__ = "action_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    tool = Column(String(64), nullable=False)
    arguments_json = Column(Text, nullable=False, default="{}")
    outcome = Column(String(16), nullable=False, default="done")     # done|failed|denied
    mode = Column(String(16), nullable=False, default=MODE_AUTO)     # auto|confirmed|schedule|event
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ChatMessage(Base):
    """Conversation history — the same dialogue in Telegram and in the web panel."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(16), nullable=False)                       # user|assistant
    content = Column(Text, nullable=False, default="")
    channel = Column(String(16), nullable=False, default="web")     # web|telegram|system
    payload_json = Column(Text, nullable=True)                      # трейсы инструментов и карточки
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class Connector(Base):
    """External service connection — personal for each family member."""
    __tablename__ = "connectors"
    __table_args__ = (UniqueConstraint("user_id", "service", name="uq_connector"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    service = Column(String(32), nullable=False)
    connected = Column(Boolean, nullable=False, default=False)
    permission = Column(String(8), nullable=False, default=CONN_OFF)
    credentials_json = Column(Text, nullable=True)


class ScheduledJob(Base):
    """Recurring agent job (утренняя сводка, вечерний итог, разбор недели)."""
    __tablename__ = "scheduled_jobs"
    __table_args__ = (UniqueConstraint("user_id", "kind", name="uq_scheduled_job"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(32), nullable=False)         # morning_digest|evening_summary|weekly_review
    at_time = Column(String(5), nullable=False, default="08:30")   # HH:MM в локальной зоне семьи
    weekday = Column(Integer, nullable=True)          # 0..6 для недельных задач
    enabled = Column(Boolean, nullable=False, default=False)
    last_run_at = Column(DateTime, nullable=True)


class FamilySettings(Base):
    """Screen «Модель и знания» — family-wide, editable by the head only."""
    __tablename__ = "family_settings"

    id = Column(Integer, primary_key=True)
    family_id = Column(Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False, unique=True)

    core_model = Column(String(16), nullable=False, default="hybrid")   # local|cloud|hybrid
    vlm_mode = Column(String(16), nullable=False, default="core")       # core|separate
    yolo_model = Column(String(16), nullable=False, default="yolov8n")
    frames_stay_home = Column(Boolean, nullable=False, default=True)
    cloud_budget_eur = Column(Integer, nullable=False, default=20)
    cloud_spent_eur = Column(Float, nullable=False, default=0.0)
    rag_sources_json = Column(Text, nullable=False, default="{}")
    accent_color = Column(String(16), nullable=False, default="#2E6E7E")
