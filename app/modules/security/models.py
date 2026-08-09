"""Security tables — shared across the whole family, scoped by family_id.

Unlike nutrition, this data is not personal: everyone in the household sees the
same cameras and the same events. Only the metadata lives in the database; the
frames themselves sit under MEDIA_ROOT and are rotated away after
`Camera.retention_days` (see retention.py).
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)

from app.core.db import Base

# --- verdicts -------------------------------------------------------------
VERDICT_NORMAL = "normal"      # обычная жизнь дома — только в лог
VERDICT_CHECK = "check"        # похоже на своих, но время или зона необычные
VERDICT_ANOMALY = "anomaly"    # похоже на постороннего

VERDICT_LABELS = {VERDICT_NORMAL: "штатно", VERDICT_CHECK: "проверить", VERDICT_ANOMALY: "аномалия"}


class Camera(Base):
    __tablename__ = "cameras"
    __table_args__ = (UniqueConstraint("family_id", "slug", name="uq_camera_slug"),)

    id = Column(Integer, primary_key=True)
    family_id = Column(Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True)

    slug = Column(String(64), nullable=False)            # имя, которым камера представляется при ingest
    label = Column(String(64), nullable=False)           # «Калитка», «Двор»
    zone = Column(String(32), nullable=False, default="улица")

    #: Выключенная камера продолжает писать события в лог, но молчит в Telegram —
    #: ровно то, что нужно для камеры, которая ловит кошек.
    notify_enabled = Column(Boolean, nullable=False, default=True)
    #: Часы, в которые движение здесь считается необычным (локальное время семьи).
    quiet_from = Column(Integer, nullable=False, default=23)
    quiet_to = Column(Integer, nullable=False, default=6)
    #: Зона, где посторонний — всегда повод сказать (например, внутри дома).
    always_notify = Column(Boolean, nullable=False, default=False)

    retention_days = Column(Integer, nullable=False, default=14)
    hint = Column(String(255), nullable=True)            # подсказка под карточкой камеры
    last_seen_at = Column(DateTime, nullable=True)

    @property
    def mode_label(self) -> str:
        return "уведомляет" if self.notify_enabled else "только лог"


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id = Column(Integer, primary_key=True)
    family_id = Column(Integer, ForeignKey("families.id", ondelete="CASCADE"), nullable=False, index=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"), nullable=False, index=True)

    happened_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    verdict = Column(String(16), nullable=False, default=VERDICT_NORMAL, index=True)
    reason = Column(String(255), nullable=True)          # почему так решили — показывается человеку

    detected_class = Column(String(32), nullable=True)   # person, car, ...
    confidence = Column(Float, nullable=True)
    area = Column(Integer, nullable=True)

    snapshot_path = Column(String(512), nullable=True)
    clip_path = Column(String(512), nullable=True)

    notified_at = Column(DateTime, nullable=True)        # когда ушло в Telegram
    resolution = Column(String(16), nullable=True)       # ours|checked — реакция семьи
    resolved_at = Column(DateTime, nullable=True)
    classified_by = Column(String(16), nullable=False, default="rules")   # rules|model
    note = Column(Text, nullable=True)

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)

    @property
    def is_anomaly(self) -> bool:
        return self.verdict in (VERDICT_ANOMALY, VERDICT_CHECK)
