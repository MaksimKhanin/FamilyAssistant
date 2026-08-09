"""Часовой пояс там, где он виден человеку: сутки питания и «тихие часы» дома.

Этот баг нашёлся на первом локальном запуске: ночная тревога в 23:14 рисовалась
как 05:17, потому что и правила, и экраны работали в UTC.
"""
from datetime import date, datetime

import pytest
from zoneinfo import ZoneInfo

from app.core import clock
from app.core.templating import ru_datetime, ru_time
from app.modules.nutrition import service as nutrition
from app.modules.nutrition.vision import MealEstimate
from app.modules.security import service as security
from app.modules.security.models import VERDICT_ANOMALY, VERDICT_NORMAL


@pytest.fixture
def moscow(monkeypatch):
    monkeypatch.setattr(clock, "LOCAL_ZONE", ZoneInfo("Europe/Moscow"))


def test_event_time_is_shown_in_family_time(moscow):
    assert ru_time(datetime(2026, 8, 9, 20, 14)) == "23:14"


def test_today_is_the_familys_today(moscow, monkeypatch):
    monkeypatch.setattr(clock, "utc_now", lambda: datetime(2026, 8, 9, 22, 30))   # 01:30 в Москве
    assert ru_datetime(datetime(2026, 8, 9, 22, 0)) == "Сегодня, 01:00"


def test_night_intruder_is_judged_by_local_hours(moscow, db, family):
    """20:14 UTC — это 23:14 дома, то есть уже тихие часы."""
    camera = security.get_or_create_camera(db, family.id, "gate", "Калитка")
    event = security.record_event(db, family.id, camera, datetime(2026, 8, 9, 20, 14),
                                  detected_class="person", confidence=0.9)
    assert event.verdict == VERDICT_ANOMALY


def test_the_same_moment_in_utc_would_have_looked_ordinary(moscow, db, family):
    """Без перевода в местное время 20:14 попало бы в «обычный вечер» и потерялось."""
    camera = security.get_or_create_camera(db, family.id, "gate", "Калитка")
    naive = security.decide(camera, "person", datetime(2026, 8, 9, 20, 14), 0.9)
    assert naive.verdict == VERDICT_NORMAL       # ровно та ошибка, которую чиним


def test_late_meal_counts_towards_the_right_day(moscow, db, head):
    """Ужин в 23:30 по-местному — это сегодняшний ужин, а не завтрашний завтрак."""
    supper = datetime(2026, 8, 9, 20, 30)        # 23:30 в Москве
    nutrition.create_draft(db, head.id, MealEstimate("Ужин", 600, 30, 20, 60), eaten_at=supper)

    stats = nutrition.period_stats(db, head.id, "day", today=date(2026, 8, 9))
    assert stats.consumed == 600

    meals = nutrition.meals_for_day(db, head.id, day=date(2026, 8, 9))
    assert [m.title for m in meals] == ["Ужин"]


def test_meal_after_local_midnight_belongs_to_the_next_day(moscow, db, head):
    night_snack = datetime(2026, 8, 9, 22, 0)    # 01:00 десятого в Москве
    nutrition.create_draft(db, head.id, MealEstimate("Ночной перекус", 200, 5, 8, 25),
                           eaten_at=night_snack)

    assert nutrition.period_stats(db, head.id, "day", today=date(2026, 8, 9)).consumed == 0
    assert nutrition.period_stats(db, head.id, "day", today=date(2026, 8, 10)).consumed == 200
