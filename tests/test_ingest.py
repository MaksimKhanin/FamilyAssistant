"""Ingest endpoint — the only door the edge worker knocks on."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.core.events import SECURITY_ANOMALY, bus
from app.main import app
from app.modules.security.models import SecurityEvent

API_KEY = "test-ingest-key"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _post(client, **fields):
    data = {"camera": "gate", "detected_class": "person", "confidence": "0.9", "area": "4200"}
    data.update({k: str(v) for k, v in fields.items()})
    return client.post(
        "/api/security/events",
        headers={"Authorization": f"Bearer {API_KEY}"},
        data=data,
        files={"snapshot": ("frame.jpg", b"not-a-real-jpeg", "image/jpeg")},
    )


def test_ingest_requires_the_shared_key(client, family):
    response = client.post("/api/security/events", data={"camera": "gate"})
    assert response.status_code == 401


def test_first_event_registers_the_camera(client, family):
    response = _post(client, captured_at=datetime(2026, 8, 9, 14, 0).isoformat())
    assert response.status_code == 200
    assert response.json()["verdict"] == "normal"

    from app.modules.security.models import Camera
    from app.core.db import SessionLocal
    with SessionLocal() as check:
        assert check.query(Camera).filter(Camera.slug == "gate").one().label == "Gate"


def test_night_person_becomes_an_anomaly_and_is_published(client, family, db):
    received = []
    bus.subscribe(SECURITY_ANOMALY, received.append)

    response = _post(client, captured_at=datetime(2026, 8, 9, 23, 14).isoformat())
    assert response.json()["verdict"] == "anomaly"

    event = db.query(SecurityEvent).one()
    assert event.snapshot_path                      # кадр сохранён на диск
    assert [p["event_id"] for p in received] == [event.id]


def test_unparseable_time_falls_back_to_now_instead_of_failing(client, family, db):
    response = _post(client, captured_at="позавчера")
    assert response.status_code == 200
    assert db.query(SecurityEvent).one().happened_at is not None


def test_ingest_without_a_family_is_a_clear_error(client, db):
    assert _post(client).status_code == 409
