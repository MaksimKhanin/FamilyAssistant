"""Архив: листание, фильтры, превью и — главное — кто какие файлы может открыть.

Отдача файла по пути из базы это ровно то место, где чужой архив утекает наружу,
если проверку забыть. Поэтому здесь её проверяют отдельно от всего остального.
"""
import pytest
from fastapi.testclient import TestClient

from app.core import media
from app.core.auth import hash_password
from app.core.clock import utc_now
from app.core.db import get_db
from app.core.models import Family
from app.main import app
from app.modules.security import service, thumbnails
from app.modules.security.filenames import KIND_PHOTO, KIND_VIDEO

try:
    import cv2                                          # noqa: F401
    HAS_CV2 = True
except ImportError:                                     # pragma: no cover
    HAS_CV2 = False


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_member(client, db, member):
    member.password_hash = hash_password("pw")
    db.commit()
    client.post("/login", data={"username": member.username, "password": "pw"},
                follow_redirects=False)
    return client


def _add(db, family_id, camera, name, kind=KIND_PHOTO, minutes_ago=0, body=b"bytes", **fields):
    """Положить файл на диск и строку в базу — как это делает ingest."""
    from datetime import timedelta
    captured = utc_now() - timedelta(minutes=minutes_ago)
    path = media.media_dir("security", camera.slug, captured.strftime("%Y-%m-%d")) / name
    path.write_bytes(body)
    return service.record_media(
        db, family_id=family_id, camera=camera, filename=name, kind=kind,
        rel_path=media.relative(path), captured_at=captured, size_bytes=len(body), **fields,
    )


@pytest.fixture
def gate(db, family):
    return service.get_or_create_camera(db, family.id, "gate", "Калитка")


def test_the_archive_lists_what_arrived(as_member, db, family, gate):
    _add(db, family.id, gate, "a.jpg")

    response = as_member.get("/security/archive")

    assert response.status_code == 200
    assert "Калитка" in response.text


def test_paging_does_not_repeat_or_lose_anything(as_member, db, family, gate):
    for i in range(service.PAGE_SIZE + 3):
        _add(db, family.id, gate, f"file-{i:02d}.jpg", minutes_ago=i)

    first, has_more = service.list_media(db, family.id, page=1)
    second, still_more = service.list_media(db, family.id, page=2)

    assert len(first) == service.PAGE_SIZE and has_more
    assert len(second) == 3 and not still_more
    assert not {i.id for i in first} & {i.id for i in second}


def test_the_camera_filter_shows_only_that_camera(as_member, db, family, gate):
    yard = service.get_or_create_camera(db, family.id, "yard", "Двор")
    _add(db, family.id, gate, "gate.jpg")
    _add(db, family.id, yard, "yard.jpg")

    items, _ = service.list_media(db, family.id, camera_id=yard.id)

    assert [i.filename for i in items] == ["yard.jpg"]


def test_only_alerts_leaves_out_the_routine_recording(db, family, gate):
    _add(db, family.id, gate, "chunk.mp4", kind=KIND_VIDEO)
    _add(db, family.id, gate, "person.jpg", is_alert=True, detected_class="person")

    items, _ = service.list_media(db, family.id, alerts_only=True)

    assert [i.filename for i in items] == ["person.jpg"]


def test_a_file_is_served_to_its_own_family(as_member, db, family, gate):
    item = _add(db, family.id, gate, "a.jpg", body=b"jpeg-bytes")

    response = as_member.get(f"/security/file/{item.id}")

    assert response.status_code == 200
    assert response.content == b"jpeg-bytes"
    # Без этого браузер даже не попробует перемотать видео.
    assert response.headers["accept-ranges"] == "bytes"


def test_a_video_can_be_seeked_not_just_downloaded(as_member, db, family, gate):
    """Часовая склеенная запись без Range означает «смотри с начала или качай целиком»."""
    item = _add(db, family.id, gate, "clip.mp4", kind=KIND_VIDEO, body=b"0123456789")

    response = as_member.get(f"/security/file/{item.id}", headers={"Range": "bytes=4-6"})

    assert response.status_code == 206
    assert response.content == b"456"
    assert response.headers["content-range"] == "bytes 4-6/10"


def test_an_open_ended_range_runs_to_the_end(as_member, db, family, gate):
    item = _add(db, family.id, gate, "clip.mp4", kind=KIND_VIDEO, body=b"0123456789")

    response = as_member.get(f"/security/file/{item.id}", headers={"Range": "bytes=7-"})

    assert response.status_code == 206
    assert response.content == b"789"


def test_a_suffix_range_asks_for_the_tail(as_member, db, family, gate):
    """mp4 с moov-атомом в конце браузер начинает читать именно так."""
    item = _add(db, family.id, gate, "clip.mp4", kind=KIND_VIDEO, body=b"0123456789")

    response = as_member.get(f"/security/file/{item.id}", headers={"Range": "bytes=-3"})

    assert response.status_code == 206
    assert response.content == b"789"


def test_a_range_past_the_end_is_refused_properly(as_member, db, family, gate):
    item = _add(db, family.id, gate, "clip.mp4", kind=KIND_VIDEO, body=b"0123456789")

    response = as_member.get(f"/security/file/{item.id}", headers={"Range": "bytes=99-200"})

    assert response.status_code == 416


def test_another_familys_file_is_not_served(as_member, db, family, gate):
    """Разные семьи на одном сервере не должны видеть архив друг друга."""
    other = Family(name="Соседи")
    db.add(other)
    db.flush()
    their_camera = service.get_or_create_camera(db, other.id, "their-gate", "Их калитка")
    theirs = _add(db, other.id, their_camera, "secret.jpg")

    assert as_member.get(f"/security/file/{theirs.id}").status_code == 404
    assert as_member.get(f"/security/media/{theirs.id}").status_code == 404


def test_a_stranger_gets_no_file_at_all(client, db, family, gate):
    item = _add(db, family.id, gate, "a.jpg")

    response = client.get(f"/security/file/{item.id}", follow_redirects=False)

    assert response.status_code in (303, 401, 403)


def test_the_page_survives_a_file_that_rotation_already_removed(as_member, db, family, gate):
    """Строка живёт дольше файла — экран должен это сказать, а не сломаться."""
    item = _add(db, family.id, gate, "a.jpg")
    media.resolve(item.rel_path).unlink()

    response = as_member.get(f"/security/media/{item.id}")

    assert response.status_code == 200
    assert "удалён по сроку хранения" in response.text


@pytest.mark.skipif(not HAS_CV2, reason="превью требуют opencv")
def test_a_thumbnail_is_smaller_than_the_frame(db, family, gate, tmp_path):
    import cv2
    import numpy as np

    original = tmp_path / "big.jpg"
    cv2.imwrite(str(original), np.full((900, 1600, 3), 120, dtype=np.uint8))

    thumb = thumbnails.generate(original, KIND_PHOTO)

    assert thumb is not None and thumb.exists()
    assert max(cv2.imread(str(thumb)).shape[:2]) == thumbnails.THUMB_MAX_DIM


def test_the_camera_tile_falls_back_to_the_last_frame(db, family, gate):
    _add(db, family.id, gate, "old.jpg", minutes_ago=90)
    newest = _add(db, family.id, gate, "new.jpg", minutes_ago=1)
    _add(db, family.id, gate, "chunk.mp4", kind=KIND_VIDEO, minutes_ago=0)   # видео в <img> не годится

    assert service.latest_media(db, family.id, gate.id).id == newest.id
