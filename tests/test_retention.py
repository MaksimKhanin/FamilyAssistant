"""Ротация архива.

Это единственное место, которое освобождает диск, и единственное, которое удаляет
файлы без спроса. Ошибка в любую сторону дорогая: не удалит — том кончится посреди
ночи; удалит лишнее — семья потеряет запись, которую как раз собиралась посмотреть.
"""
from datetime import timedelta

import pytest

from app.core import media
from app.core.clock import utc_now
from app.modules.security import retention, service
from app.modules.security.filenames import KIND_PHOTO, KIND_VIDEO
from app.modules.security.models import MediaItem, SecurityEvent


@pytest.fixture
def gate(db, family):
    camera = service.get_or_create_camera(db, family.id, "gate", "Калитка")
    camera.retention_days = 14
    db.commit()
    return camera


def _add(db, family_id, camera, name, days_ago, kind=KIND_PHOTO, with_thumb=False):
    captured = utc_now() - timedelta(days=days_ago)
    directory = media.media_dir("security", camera.slug, captured.strftime("%Y-%m-%d"))
    path = directory / name
    path.write_bytes(b"bytes")

    thumb_rel = None
    if with_thumb:
        thumb = directory / f"{path.stem}_thumb.jpg"
        thumb.write_bytes(b"thumb")
        thumb_rel = media.relative(thumb)

    return service.record_media(
        db, family_id=family_id, camera=camera, filename=name, kind=kind,
        rel_path=media.relative(path), thumb_rel_path=thumb_rel, captured_at=captured,
    )


def test_old_media_goes_away_with_its_file_and_thumbnail(db, family, gate):
    old = _add(db, family.id, gate, "old.jpg", days_ago=20, with_thumb=True)
    file_path, thumb_path = media.resolve(old.rel_path), media.resolve(old.thumb_rel_path)

    retention.rotate(db)

    assert db.query(MediaItem).count() == 0
    assert not file_path.exists() and not thumb_path.exists()


def test_fresh_media_is_left_alone(db, family, gate):
    fresh = _add(db, family.id, gate, "fresh.mp4", days_ago=2, kind=KIND_VIDEO)

    retention.rotate(db)

    assert db.query(MediaItem).count() == 1
    assert media.resolve(fresh.rel_path).exists()


def test_each_camera_keeps_its_own_window(db, family, gate):
    """У камеры на кошек срок короткий, у калитки — длинный. Общей отсечки быть не должно."""
    yard = service.get_or_create_camera(db, family.id, "yard", "Двор")
    yard.retention_days = 3
    db.commit()

    _add(db, family.id, gate, "gate.jpg", days_ago=10)
    _add(db, family.id, yard, "yard.jpg", days_ago=10)

    retention.rotate(db)

    assert [i.filename for i in db.query(MediaItem).all()] == ["gate.jpg"]


def test_an_event_outlives_its_frame(db, family, gate):
    """«Что случилось» семья помнит дольше, чем хранится сам кадр."""
    captured = utc_now() - timedelta(days=20)
    path = media.media_dir("security", gate.slug, "2026-01-01") / "person.jpg"
    path.write_bytes(b"frame")
    service.record_event(db, family.id, gate, captured, detected_class="person",
                         confidence=0.9, area=4000, snapshot_path=str(path))

    retention.rotate(db)

    event = db.query(SecurityEvent).one()
    assert event.snapshot_path is None
    assert not path.exists()


def test_the_day_directory_does_not_stay_behind_empty(db, family, gate):
    """Каталоги нарезаны по дням: без уборки их накапливается по одному в сутки на камеру."""
    old = _add(db, family.id, gate, "old.jpg", days_ago=20)
    directory = media.resolve(old.rel_path).parent

    retention.rotate(db)

    assert not directory.exists()


def test_a_missing_file_does_not_stop_the_pass(db, family, gate):
    """Файл могли унести руками или бэкапом — это не повод не почистить остальное."""
    first = _add(db, family.id, gate, "gone.jpg", days_ago=20)
    media.resolve(first.rel_path).unlink()
    _add(db, family.id, gate, "also-old.jpg", days_ago=20)

    retention.rotate(db)

    assert db.query(MediaItem).count() == 0
