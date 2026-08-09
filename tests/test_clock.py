"""Время: в базе UTC, у человека — его часы.

Тесты гоняются с TIMEZONE=UTC (см. conftest), поэтому здесь пояс подставляется
явно — иначе проверять было бы нечего.
"""
from datetime import date, datetime

import pytest

from app.core import clock


@pytest.fixture
def moscow(monkeypatch):
    """Семья живёт в UTC+3."""
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(clock, "LOCAL_ZONE", ZoneInfo("Europe/Moscow"))


def test_stored_utc_is_shown_in_family_time(moscow):
    stored = datetime(2026, 8, 9, 20, 14)          # 20:14 UTC
    assert clock.to_local(stored) == datetime(2026, 8, 9, 23, 14)


def test_conversion_is_reversible(moscow):
    stored = datetime(2026, 8, 9, 20, 14)
    assert clock.to_utc(clock.to_local(stored)) == stored


def test_late_evening_belongs_to_the_right_day(moscow):
    """23:14 по-местному — это ещё сегодня, хотя в UTC уже 20:14 того же дня."""
    assert clock.local_date(datetime(2026, 8, 9, 20, 14)) == date(2026, 8, 9)


def test_after_midnight_local_is_already_tomorrow(moscow):
    """01:30 у семьи — это следующий день, хотя в UTC ещё 22:30 предыдущего."""
    assert clock.local_date(datetime(2026, 8, 9, 22, 30)) == date(2026, 8, 10)


def test_day_bounds_cover_the_local_day_in_utc(moscow):
    start, end = clock.day_bounds_utc(date(2026, 8, 9))
    assert start == datetime(2026, 8, 8, 21, 0)     # полночь в Москве
    assert end == datetime(2026, 8, 9, 21, 0)
    assert (end - start).total_seconds() == 24 * 3600


def test_unknown_timezone_falls_back_to_utc_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(clock.settings, "timezone", "Средиземье/Шир")
    assert clock._resolve_zone() is not None


def test_none_survives_conversion():
    assert clock.to_local(None) is None
    assert clock.local_date(None) is None
