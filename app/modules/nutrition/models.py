"""Nutrition tables — strictly personal data, always scoped by user_id.

Calorie figures here are estimates by construction (a photo of a plate cannot give
a medical calculation), which is why every meal carries an explicit `status` and
`confidence`: the interface has to be able to say «≈ оценка» rather than presenting
a number as fact.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.core.db import Base

# --- meal status ---
STATUS_DRAFT = "draft"          # авто-оценка, ждёт подтверждения
STATUS_CONFIRMED = "confirmed"  # человек согласился с оценкой
STATUS_CORRECTED = "corrected"  # человек поправил цифры

STATUS_LABELS = {
    STATUS_DRAFT: "≈ оценка",
    STATUS_CONFIRMED: "подтверждено",
    STATUS_CORRECTED: "скорректировано вручную",
}

SOURCE_PHOTO = "photo"
SOURCE_TEXT = "text"

# --- goals ---
GOAL_LOSS = "loss"
GOAL_KEEP = "keep"
GOAL_GAIN = "gain"

GOAL_LABELS = {GOAL_LOSS: "снижение веса", GOAL_KEEP: "поддержание", GOAL_GAIN: "набор веса"}

#: kcal per unit, matching the live recalculation on the «Активность» screen.
ACTIVITY_KCAL = {"steps": 0.04, "walk": 4.0, "workout": 8.0, "bike": 7.0}
ACTIVITY_LABELS = {"steps": "Шаги", "walk": "Прогулка", "workout": "Тренировка", "bike": "Велосипед"}
ACTIVITY_UNITS = {"steps": "шагов", "walk": "мин", "workout": "мин", "bike": "мин"}


class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    eaten_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    source = Column(String(8), nullable=False, default=SOURCE_TEXT)   # photo|text
    status = Column(String(16), nullable=False, default=STATUS_DRAFT)

    title = Column(String(128), nullable=False, default="Приём пищи")
    kcal = Column(Integer, nullable=False, default=0)
    protein = Column(Integer, nullable=False, default=0)
    fat = Column(Integer, nullable=False, default=0)
    carbs = Column(Integer, nullable=False, default=0)

    portion = Column(String(128), nullable=True)
    confidence = Column(String(8), nullable=True)      # low|medium|high — насколько уверена оценка
    raw_input = Column(Text, nullable=True)            # что человек написал
    image_path = Column(String(512), nullable=True)
    note = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_estimate(self) -> bool:
        return self.status == STATUS_DRAFT


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    happened_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    kind = Column(String(16), nullable=False, default="steps")
    value = Column(Float, nullable=False, default=0)     # шаги или минуты
    kcal = Column(Integer, nullable=False, default=0)    # оценка потраченного
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def label(self) -> str:
        return ACTIVITY_LABELS.get(self.kind, self.kind)

    @property
    def unit(self) -> str:
        return ACTIVITY_UNITS.get(self.kind, "")


class NutritionProfile(Base):
    __tablename__ = "nutrition_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)

    daily_kcal = Column(Integer, nullable=False, default=2100)
    goal = Column(String(8), nullable=False, default=GOAL_KEEP)
    height_cm = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def goal_label(self) -> str:
        return GOAL_LABELS.get(self.goal, self.goal)
