"""Ingest endpoint — the only door the recorder knocks on.

Метаданные съёмки рекордер передаёт именем файла, а не полями формы, поэтому
имена в тестах настоящие — ровно те, что он собирает у себя.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.core.events import SECURITY_ANOMALY, bus
from app.main import app
from app.modules.security.models import Camera, MediaItem, SecurityEvent

API_KEY = "test-ingest-key"

#: Срабатывание YOLO: год двухзначный, время локальное, метаданные в имени.
ALERT_NIGHT = "26-08-09T-23-14-05_captured_5200_0.8600_person_done_post.jpg"
ALERT_DAY = "26-08-09T-09-40-00_captured_4800_0.9100_person_done_post.jpg"
#: Штатный чанк записи: год четырёхзначный, никаких метаданных.
VIDEO_CHUNK = "2026-08-09_23-14-00_gate_video_done_post.mp4"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post(client, filename=ALERT_DAY, payload=b"not-a-real-jpeg", **fields):
    data = {"camera": "gate", "filename": filename}
    if filename.endswith(".jpg") and "captured" in filename:
        data.update(detected_class="person", confidence="0.86", area="5200")
    data.update({k: str(v) for k, v in fields.items()})
    return client.post(
        "/api/security/media",
        headers={"Authorization": f"Bearer {API_KEY}"},
        data=data,
        files={"file": (filename, payload, "application/octet-stream")},
    )


def test_ingest_requires_the_shared_key(client, family):
    response = client.post("/api/security/media", data={"camera": "gate", "filename": ALERT_DAY})
    assert response.status_code == 401


def test_first_file_registers_the_camera(client, family, db):
    response = _post(client)
    assert response.status_code == 200
    assert db.query(Camera).filter(Camera.slug == "gate").one().label == "Gate"


def test_time_comes_from_the_filename_not_from_the_clock(client, family, db):
    """Файл мог пролежать на диске часы — важно, когда он снят, а не когда доехал."""
    _post(client, filename=ALERT_NIGHT)

    item = db.query(MediaItem).one()
    assert item.captured_at.date() == datetime(2026, 8, 9).date()
    assert item.kind == "photo"


def test_a_video_chunk_lands_in_the_archive_and_wakes_nobody(client, family, db):
    """Непрерывная запись — это архив. Ни события, ни уведомления она порождать не должна."""
    received = []
    bus.subscribe(SECURITY_ANOMALY, received.append)

    response = _post(client, filename=VIDEO_CHUNK, payload=b"fake-mp4", is_alert=True)

    assert response.json()["kind"] == "video"
    assert db.query(MediaItem).one().kind == "video"
    assert db.query(SecurityEvent).count() == 0
    assert received == []


def test_night_person_becomes_an_anomaly_and_is_published(client, family, db):
    received = []
    bus.subscribe(SECURITY_ANOMALY, received.append)

    response = _post(client, filename=ALERT_NIGHT)
    assert response.json()["verdict"] == "anomaly"

    event = db.query(SecurityEvent).one()
    assert event.snapshot_path                       # кадр сохранён на диск
    assert [p["event_id"] for p in received] == [event.id]
    # Запись архива знает про своё событие — со страницы файла можно уйти в ленту.
    assert db.query(MediaItem).one().event_id == event.id


def test_an_alarm_clip_attaches_itself_to_the_event(client, family, db):
    """Снимок и видео приезжают отдельными файлами; связать их можно только по времени."""
    _post(client, filename=ALERT_NIGHT)
    _post(client, filename=VIDEO_CHUNK, payload=b"fake-mp4", is_alert=True)

    event = db.query(SecurityEvent).one()
    assert event.clip_path and event.clip_path.endswith(".mp4")


def test_a_clip_that_arrives_first_is_adopted_by_its_event(client, family, db):
    """Обычный порядок: рекордер обходит папку по алфавиту, и видео уезжает раньше снимка."""
    _post(client, filename=VIDEO_CHUNK, payload=b"fake-mp4", is_alert=True)
    _post(client, filename=ALERT_NIGHT)

    event = db.query(SecurityEvent).one()
    assert event.clip_path and event.clip_path.endswith(".mp4")
    clip = db.query(MediaItem).filter(MediaItem.kind == "video").one()
    assert clip.event_id == event.id


def test_a_clip_from_another_hour_is_left_alone(client, family, db):
    """Иначе к тревоге прицепился бы случайный кусок записи из другого времени."""
    _post(client, filename="2026-08-09_10-00-00_gate_video_done_post_alarm.mp4",
          payload=b"fake-mp4", is_alert=True)
    _post(client, filename=ALERT_NIGHT)

    assert db.query(SecurityEvent).one().clip_path is None


def test_the_same_file_twice_is_stored_once(client, family, db):
    """Рекордер пересканирует папку каждые несколько секунд — дубли неизбежны."""
    first = _post(client, filename=ALERT_NIGHT)
    second = _post(client, filename=ALERT_NIGHT)

    assert second.json() == {"status": "duplicate", "id": first.json()["id"]}
    assert db.query(MediaItem).count() == 1
    assert db.query(SecurityEvent).count() == 1


def test_a_filename_cannot_climb_out_of_the_media_root(client, family, db):
    """Имя приходит по сети и участвует в пути — «починить» его молча нельзя."""
    response = _post(client, filename="../../etc/passwd")

    assert response.status_code == 400
    assert db.query(MediaItem).count() == 0


def test_an_unknown_filename_still_gets_archived(client, family, db):
    """Чужое имя — не повод потерять файл: время просто берётся текущее."""
    response = _post(client, filename="snapshot.jpg")

    assert response.status_code == 200
    assert db.query(MediaItem).one().captured_at is not None


def test_an_empty_upload_is_refused(client, family, db):
    assert _post(client, payload=b"").status_code == 400
    assert db.query(MediaItem).count() == 0


def test_ingest_without_a_family_is_a_clear_error(client, db):
    assert _post(client).status_code == 409
