"""Nutrition: estimates stay estimates until a human says otherwise."""
from datetime import datetime, timedelta

from app.modules.nutrition import service
from app.modules.nutrition.models import STATUS_CONFIRMED, STATUS_CORRECTED, STATUS_DRAFT
from app.modules.nutrition.vision import MealEstimate


def estimate(**overrides) -> MealEstimate:
    defaults = dict(title="Овсянка", kcal=320, protein=12, fat=14, carbs=34)
    defaults.update(overrides)
    return MealEstimate(**defaults)


def test_a_new_meal_is_a_draft_marked_as_an_estimate(db, member):
    meal = service.create_draft(db, member.id, estimate())
    assert meal.status == STATUS_DRAFT
    assert meal.is_estimate
    assert meal.status_label == "≈ оценка"


def test_confirming_without_changes_keeps_the_numbers(db, member):
    meal = service.create_draft(db, member.id, estimate())
    confirmed = service.confirm_meal(db, member.id, meal.id, {})
    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.kcal == 320


def test_correcting_a_number_marks_the_record_as_hand_corrected(db, member):
    meal = service.create_draft(db, member.id, estimate())
    corrected = service.confirm_meal(db, member.id, meal.id, {"kcal": 400})
    assert corrected.status == STATUS_CORRECTED
    assert corrected.kcal == 400


def test_meals_belong_to_their_owner(db, member, other):
    meal = service.create_draft(db, member.id, estimate())
    assert service.get_meal(db, other.id, meal.id) is None
    assert service.confirm_meal(db, other.id, meal.id, {}) is None


def test_activity_estimates_use_the_published_coefficients(db, member):
    assert service.estimate_activity_kcal("steps", 6200) == 248
    assert service.estimate_activity_kcal("workout", 45) == 360
    assert service.estimate_activity_kcal("unknown", 100) == 0


def test_daily_balance_is_eaten_minus_moved(db, member):
    service.create_draft(db, member.id, estimate(kcal=500))
    service.create_draft(db, member.id, estimate(kcal=700))
    service.log_activity(db, member.id, "walk", 30)          # 120 ккал

    stats = service.period_stats(db, member.id, "day")
    assert stats.consumed == 1200
    assert stats.burned == 120
    assert stats.balance == 1080


def test_week_view_has_one_bucket_per_day_oldest_first(db, member):
    today = datetime.utcnow()
    service.create_draft(db, member.id, estimate(kcal=100), eaten_at=today)
    service.create_draft(db, member.id, estimate(kcal=200), eaten_at=today - timedelta(days=3))

    stats = service.period_stats(db, member.id, "week")
    assert len(stats.days) == 7
    assert stats.days[-1].consumed == 100
    assert stats.days[3].consumed == 200
    assert stats.consumed == 300


def test_profile_norm_is_clamped_to_the_slider_range(db, member):
    assert service.update_profile(db, member.id, daily_kcal=99999).daily_kcal == 3400
    assert service.update_profile(db, member.id, daily_kcal=10).daily_kcal == 1200
