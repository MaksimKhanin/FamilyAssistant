"""Nutrition: estimates stay estimates until a human says otherwise."""
from datetime import datetime, timedelta

from app.core.clock import local_today
from app.modules.nutrition import service
from app.modules.nutrition.models import STATUS_CONFIRMED, STATUS_CORRECTED, STATUS_DRAFT
from app.modules.nutrition.vision import MealEstimate


def estimate(**overrides) -> MealEstimate:
    defaults = dict(title="Овсянка", kcal=320, protein=12, fat=14, carbs=34)
    defaults.update(overrides)
    return MealEstimate(**defaults)


def test_a_new_meal_is_a_draft_marked_as_an_estimate(db, head):
    meal = service.create_draft(db, head.id, estimate())
    assert meal.status == STATUS_DRAFT
    assert meal.is_estimate
    assert meal.status_label == "≈ оценка"


def test_confirming_without_changes_keeps_the_numbers(db, head):
    meal = service.create_draft(db, head.id, estimate())
    confirmed = service.confirm_meal(db, head.id, meal.id, {})
    assert confirmed.status == STATUS_CONFIRMED
    assert confirmed.kcal == 320


def test_correcting_a_number_marks_the_record_as_hand_corrected(db, head):
    meal = service.create_draft(db, head.id, estimate())
    corrected = service.confirm_meal(db, head.id, meal.id, {"kcal": 400})
    assert corrected.status == STATUS_CORRECTED
    assert corrected.kcal == 400


def test_meals_belong_to_their_owner(db, head, member):
    meal = service.create_draft(db, head.id, estimate())
    assert service.get_meal(db, member.id, meal.id) is None
    assert service.confirm_meal(db, member.id, meal.id, {}) is None


def test_activity_estimates_use_the_published_coefficients(db, head):
    assert service.estimate_activity_kcal("steps", 6200) == 248
    assert service.estimate_activity_kcal("workout", 45) == 360
    assert service.estimate_activity_kcal("unknown", 100) == 0


def test_daily_balance_is_eaten_minus_moved(db, head):
    service.create_draft(db, head.id, estimate(kcal=500))
    service.create_draft(db, head.id, estimate(kcal=700))
    service.log_activity(db, head.id, "walk", 30)          # 120 ккал

    stats = service.period_stats(db, head.id, "day")
    assert stats.consumed == 1200
    assert stats.burned == 120
    assert stats.balance == 1080


def test_week_view_has_one_bucket_per_day_oldest_first(db, head):
    today = datetime.utcnow()
    service.create_draft(db, head.id, estimate(kcal=100), eaten_at=today)
    service.create_draft(db, head.id, estimate(kcal=200), eaten_at=today - timedelta(days=3))

    stats = service.period_stats(db, head.id, "week")
    assert len(stats.days) == 7
    assert stats.days[-1].consumed == 100
    assert stats.days[3].consumed == 200
    assert stats.consumed == 300


def test_profile_norm_is_clamped_to_the_slider_range(db, head):
    assert service.update_profile(db, head.id, daily_kcal=99999).daily_kcal == 3400
    assert service.update_profile(db, head.id, daily_kcal=10).daily_kcal == 1200


# --- журнал и чистка ------------------------------------------------------

def test_the_journal_shows_days_freshest_first_with_their_records(db, head):
    """Столбик графика должен раскрываться в строки, из которых он сложился."""
    today = datetime.utcnow()
    service.create_draft(db, head.id, estimate(kcal=100), eaten_at=today)
    service.create_draft(db, head.id, estimate(kcal=200), eaten_at=today - timedelta(days=2))
    service.log_activity(db, head.id, "walk", 30, happened_at=today)

    days = service.records_for_period(db, head.id, "week")

    assert [d.day for d in days] == sorted((d.day for d in days), reverse=True)
    assert len(days) == 2, "пустые дни в списке не нужны — их нечем поправить"
    assert days[0].consumed == 100
    assert days[0].burned == 120
    assert days[0].count == 2


def test_the_journal_keeps_to_its_window(db, head):
    service.create_draft(db, head.id, estimate(), eaten_at=datetime.utcnow() - timedelta(days=9))

    assert service.records_for_period(db, head.id, "week") == []
    assert len(service.records_for_period(db, head.id, "month")) == 1


def test_clearing_one_day_leaves_the_neighbours_alone(db, head):
    today = datetime.utcnow()
    service.create_draft(db, head.id, estimate(kcal=100), eaten_at=today)
    service.create_draft(db, head.id, estimate(kcal=200), eaten_at=today - timedelta(days=1))

    removed = service.clear_day(db, head.id, local_today())

    assert (removed.meals, removed.activity) == (1, 0)
    assert service.period_stats(db, head.id, "week").consumed == 200


def test_clearing_a_period_takes_both_halves_of_the_balance(db, head):
    service.create_draft(db, head.id, estimate(kcal=500))
    service.log_activity(db, head.id, "walk", 30)

    removed = service.clear_period(db, head.id, "week")

    assert (removed.meals, removed.activity) == (1, 1)
    assert removed.words == "1 приём пищи и 1 запись активности"
    stats = service.period_stats(db, head.id, "week")
    assert (stats.consumed, stats.burned) == (0, 0)


def test_clearing_only_meals_keeps_the_activity(db, head):
    service.create_draft(db, head.id, estimate(kcal=500))
    service.log_activity(db, head.id, "walk", 30)

    removed = service.clear_period(db, head.id, "week", service.WHAT_MEALS)

    assert (removed.meals, removed.activity) == (1, 0)
    assert service.period_stats(db, head.id, "week").burned == 120


def test_clearing_never_reaches_another_persons_records(db, head, member):
    service.create_draft(db, head.id, estimate(kcal=500))

    assert not service.clear_period(db, member.id, "month")
    assert service.period_stats(db, head.id, "day").consumed == 500


def test_a_removed_meal_takes_its_photo_off_the_disk(db, head, tmp_path):
    """Строка ушла — снимок тарелки уже никто не откроет, а место он занимает."""
    photo = tmp_path / "plate.jpg"
    photo.write_bytes(b"jpeg")
    meal = service.create_draft(db, head.id, estimate(), image_path=str(photo))

    service.delete_meal(db, head.id, meal.id)

    assert not photo.exists()


def test_clearing_a_period_takes_the_photos_too(db, head, tmp_path):
    photo = tmp_path / "plate.jpg"
    photo.write_bytes(b"jpeg")
    service.create_draft(db, head.id, estimate(), image_path=str(photo))

    service.clear_period(db, head.id, "day")

    assert not photo.exists()


def test_nothing_removed_says_so(db, head):
    removed = service.clear_period(db, head.id, "month")

    assert not removed
    assert removed.words == "ничего"
