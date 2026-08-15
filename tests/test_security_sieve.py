"""Сито — ступень отбора, которая решает, услышит ли семья о событии с камеры."""
from datetime import datetime

import pytest

from app.modules.security.models import (
    VERDICT_ANOMALY, VERDICT_CHECK, VERDICT_NORMAL, Camera,
)
from app.modules.security.service import decide


def camera(**overrides) -> Camera:
    defaults = dict(family_id=1, slug="gate", label="Калитка", zone="улица",
                    notify_enabled=True, quiet_from=23, quiet_to=6, always_notify=False)
    defaults.update(overrides)
    return Camera(**defaults)


def test_person_during_the_day_is_just_life(db=None):
    verdict = decide(camera(), "person", datetime(2026, 8, 9, 14, 0), confidence=0.9)
    assert verdict.verdict == VERDICT_NORMAL


def test_person_at_night_is_worth_a_look():
    verdict = decide(camera(), "person", datetime(2026, 8, 9, 23, 14), confidence=0.82)
    assert verdict.verdict == VERDICT_ANOMALY
    assert "вне обычного времени" in verdict.reason


@pytest.mark.parametrize("hour", [23, 0, 3, 5])
def test_quiet_window_wraps_around_midnight(hour):
    verdict = decide(camera(), "person", datetime(2026, 8, 9, hour, 30), confidence=0.9)
    assert verdict.verdict == VERDICT_ANOMALY


@pytest.mark.parametrize("hour", [6, 12, 22])
def test_outside_the_quiet_window_nothing_happens(hour):
    verdict = decide(camera(), "person", datetime(2026, 8, 9, hour, 30), confidence=0.9)
    assert verdict.verdict == VERDICT_NORMAL


def test_indoor_zone_always_notifies():
    indoor = camera(zone="прихожая", always_notify=True)
    verdict = decide(indoor, "person", datetime(2026, 8, 9, 14, 0), confidence=0.9)
    assert verdict.verdict == VERDICT_ANOMALY


def test_a_cat_is_not_an_event():
    verdict = decide(camera(), "cat", datetime(2026, 8, 9, 23, 30), confidence=0.99)
    assert verdict.verdict == VERDICT_NORMAL


def test_unsure_detection_is_dropped():
    verdict = decide(camera(), "person", datetime(2026, 8, 9, 23, 30), confidence=0.2)
    assert verdict.verdict == VERDICT_NORMAL


def test_car_at_night_is_only_worth_checking():
    """Deliberately softer than a person: «проверить», not «аномалия»."""
    verdict = decide(camera(), "car", datetime(2026, 8, 9, 2, 0), confidence=0.8)
    assert verdict.verdict == VERDICT_CHECK
