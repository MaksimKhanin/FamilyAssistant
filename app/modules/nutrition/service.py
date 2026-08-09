"""Nutrition domain logic: meals, activity, daily balance, statistics."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core import media
from app.core.clock import day_bounds_utc, local_date, local_today, utc_now
from app.modules.nutrition.models import (
    ACTIVITY_KCAL, GOAL_KEEP, SOURCE_PHOTO, SOURCE_TEXT, STATUS_CONFIRMED,
    STATUS_CORRECTED, STATUS_DRAFT, ActivityLog, Meal, NutritionProfile,
)
from app.modules.nutrition.vision import MealEstimate

PERIODS = {"day": 1, "week": 7, "month": 30}
PERIOD_LABELS = {"day": "День", "week": "Неделя", "month": "Месяц"}


# --- profile --------------------------------------------------------------

def get_profile(db: Session, user_id: int) -> NutritionProfile:
    profile = db.query(NutritionProfile).filter(NutritionProfile.user_id == user_id).one_or_none()
    if profile is None:
        profile = NutritionProfile(user_id=user_id, daily_kcal=2100, goal=GOAL_KEEP)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def update_profile(db: Session, user_id: int, daily_kcal: int = None, goal: str = None,
                   height_cm: int = None, weight_kg: float = None) -> NutritionProfile:
    profile = get_profile(db, user_id)
    if daily_kcal is not None:
        profile.daily_kcal = max(1200, min(3400, int(daily_kcal)))
    if goal is not None:
        profile.goal = goal
    if height_cm is not None:
        profile.height_cm = height_cm
    if weight_kg is not None:
        profile.weight_kg = weight_kg
    db.commit()
    db.refresh(profile)
    return profile


# --- meals ----------------------------------------------------------------

def save_image(image_bytes: bytes, user_id: int) -> str:
    """Store a meal photo under MEDIA_ROOT/meals/<user>/<date>/ and return its path."""
    return media.store_bytes(image_bytes, "meals", str(user_id),
                             datetime.utcnow().strftime("%Y-%m-%d"))


def create_draft(db: Session, user_id: int, estimate: MealEstimate, source: str = SOURCE_TEXT,
                 raw_input: str = None, image_path: str = None, eaten_at: datetime = None) -> Meal:
    meal = Meal(
        user_id=user_id,
        eaten_at=eaten_at or datetime.utcnow(),
        source=source if source in (SOURCE_PHOTO, SOURCE_TEXT) else SOURCE_TEXT,
        status=STATUS_DRAFT,
        title=estimate.title,
        kcal=estimate.kcal,
        protein=estimate.protein,
        fat=estimate.fat,
        carbs=estimate.carbs,
        portion=estimate.portion,
        confidence=estimate.confidence,
        raw_input=raw_input,
        image_path=image_path,
        note=estimate.note,
    )
    db.add(meal)
    db.commit()
    db.refresh(meal)
    return meal


def get_meal(db: Session, user_id: int, meal_id: int) -> Optional[Meal]:
    meal = db.get(Meal, meal_id)
    return meal if meal is not None and meal.user_id == user_id else None


def confirm_meal(db: Session, user_id: int, meal_id: int, corrections: dict = None) -> Optional[Meal]:
    """Finalise a draft. Any correction flips the status to «скорректировано вручную»."""
    meal = get_meal(db, user_id, meal_id)
    if meal is None:
        return None

    changed = False
    for field in ("kcal", "protein", "fat", "carbs"):
        value = (corrections or {}).get(field)
        if value is None:
            continue
        try:
            value = max(0, int(round(float(value))))
        except (TypeError, ValueError):
            continue
        if value != getattr(meal, field):
            setattr(meal, field, value)
            changed = True

    title = (corrections or {}).get("title")
    if title and title.strip() and title.strip() != meal.title:
        meal.title = title.strip()[:128]
        changed = True

    meal.status = STATUS_CORRECTED if changed else STATUS_CONFIRMED
    db.commit()
    db.refresh(meal)
    return meal


def delete_meal(db: Session, user_id: int, meal_id: int) -> bool:
    meal = get_meal(db, user_id, meal_id)
    if meal is None:
        return False
    db.delete(meal)
    db.commit()
    return True


def meals_for_day(db: Session, user_id: int, day: date = None) -> List[Meal]:
    start, end = day_bounds_utc(day or local_today())
    return (
        db.query(Meal)
        .filter(Meal.user_id == user_id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .order_by(Meal.eaten_at)
        .all()
    )


# --- activity -------------------------------------------------------------

def estimate_activity_kcal(kind: str, value: float) -> int:
    return int(round(ACTIVITY_KCAL.get(kind, 0.0) * float(value or 0)))


def log_activity(db: Session, user_id: int, kind: str, value: float,
                 happened_at: datetime = None) -> ActivityLog:
    entry = ActivityLog(
        user_id=user_id,
        kind=kind,
        value=float(value or 0),
        kcal=estimate_activity_kcal(kind, value),
        happened_at=happened_at or datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def activity_for_day(db: Session, user_id: int, day: date = None) -> List[ActivityLog]:
    start, end = day_bounds_utc(day or local_today())
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id,
                ActivityLog.happened_at >= start,
                ActivityLog.happened_at < end)
        .order_by(ActivityLog.happened_at)
        .all()
    )


# --- statistics -----------------------------------------------------------

@dataclass
class DayTotals:
    day: date
    consumed: int = 0
    burned: int = 0
    protein: int = 0
    fat: int = 0
    carbs: int = 0

    @property
    def balance(self) -> int:
        return self.consumed - self.burned


@dataclass
class PeriodStats:
    period: str
    days: List[DayTotals]
    norm: int

    @property
    def consumed(self) -> int:
        return sum(d.consumed for d in self.days)

    @property
    def burned(self) -> int:
        return sum(d.burned for d in self.days)

    @property
    def balance(self) -> int:
        return self.consumed - self.burned

    @property
    def avg_consumed(self) -> int:
        return round(self.consumed / len(self.days)) if self.days else 0

    @property
    def macros(self) -> Dict[str, int]:
        return {
            "protein": sum(d.protein for d in self.days),
            "fat": sum(d.fat for d in self.days),
            "carbs": sum(d.carbs for d in self.days),
        }

    @property
    def today(self) -> DayTotals:
        return self.days[-1] if self.days else DayTotals(day=local_today())


def period_stats(db: Session, user_id: int, period: str = "day", today: date = None) -> PeriodStats:
    """Per-day consumed/burned totals for the requested window, oldest day first."""
    span = PERIODS.get(period, 1)
    today = today or local_today()
    first_day = today - timedelta(days=span - 1)
    start, _ = day_bounds_utc(first_day)

    buckets = {first_day + timedelta(days=i): DayTotals(day=first_day + timedelta(days=i)) for i in range(span)}

    for meal in db.query(Meal).filter(Meal.user_id == user_id, Meal.eaten_at >= start).all():
        bucket = buckets.get(local_date(meal.eaten_at))
        if bucket is None:
            continue
        bucket.consumed += meal.kcal
        bucket.protein += meal.protein
        bucket.fat += meal.fat
        bucket.carbs += meal.carbs

    for entry in db.query(ActivityLog).filter(ActivityLog.user_id == user_id,
                                              ActivityLog.happened_at >= start).all():
        bucket = buckets.get(local_date(entry.happened_at))
        if bucket is not None:
            bucket.burned += entry.kcal

    profile = get_profile(db, user_id)
    return PeriodStats(period=period, days=[buckets[k] for k in sorted(buckets)], norm=profile.daily_kcal)


def recent_meal_titles(db: Session, user_id: int, days: int = 14, limit: int = 25) -> List[str]:
    since = utc_now() - timedelta(days=days)
    rows = (
        db.query(Meal)
        .filter(Meal.user_id == user_id, Meal.eaten_at >= since)
        .order_by(Meal.eaten_at.desc())
        .limit(limit)
        .all()
    )
    return [f"{m.title} (≈{m.kcal} ккал)" for m in rows]
