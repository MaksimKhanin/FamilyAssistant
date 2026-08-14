"""Уборка за собой: «я всё видел» и «убери старое».

Два действия пачкой, и они намеренно разной цены. «Просмотрено» ничего не теряет —
событие остаётся в ленте вместе с вердиктом, гаснет только значок; поэтому его
можно нажимать не думая. «Убрать из архива» стирает файлы с диска, и вернуть их
неоткуда — поэтому у него нет пункта «всё» и агент спрашивает разрешения.

Проверяется здесь ровно граница между ними: что уборка задевает, а что нет.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.agent.registry import ToolContext
from app.core import media
from app.core.auth import hash_password
from app.core.clock import utc_now
from app.core.db import get_db
from app.main import app
from app.modules.security import retention, service, tools
from app.modules.security.filenames import KIND_PHOTO
from app.modules.security.models import (
    RESOLUTION_OURS, RESOLUTION_SEEN, VERDICT_ANOMALY, MediaItem, SecurityEvent,
)


@pytest.fixture
def gate(db, family):
    return service.get_or_create_camera(db, family.id, "gate", "Калитка")


@pytest.fixture
def yard(db, family):
    return service.get_or_create_camera(db, family.id, "yard", "Двор")


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


def _alarm(db, family_id, camera, hours_ago=1, verdict=VERDICT_ANOMALY):
    """Тревога, которую никто ещё не разбирал."""
    event = SecurityEvent(family_id=family_id, camera_id=camera.id,
                          happened_at=utc_now() - timedelta(hours=hours_ago),
                          verdict=verdict, reason="кто-то у калитки")
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _stored(db, family_id, camera, name, days_ago=0, body=b"bytes"):
    """Файл на диске и строка в архиве — как это делает ingest."""
    captured = utc_now() - timedelta(days=days_ago)
    path = media.media_dir("security", camera.slug, captured.strftime("%Y-%m-%d")) / name
    path.write_bytes(body)
    return service.record_media(db, family_id=family_id, camera=camera, filename=name,
                                kind=KIND_PHOTO, rel_path=media.relative(path),
                                captured_at=captured, size_bytes=len(body))


def _ctx(db, user):
    return ToolContext(db=db, actor=user, subject=user)


# --- «просмотрено» пачкой ---------------------------------------------------

def test_marking_everything_seen_puts_out_the_badge(db, family, gate):
    for hours in (1, 5, 30):
        _alarm(db, family.id, gate, hours_ago=hours)

    assert service.mark_seen(db, family.id) == 3

    assert service.unseen_count(db, family.id) == 0
    assert service.anomaly_count(db, family.id, days=7) == 0


def test_the_events_stay_in_the_feed_with_their_verdict(db, family, gate):
    """Просмотрено — это не «удалено»: лента и вердикт остаются на месте."""
    event = _alarm(db, family.id, gate)

    service.mark_seen(db, family.id)

    db.refresh(event)
    assert event.verdict == VERDICT_ANOMALY
    assert event.resolution == RESOLUTION_SEEN
    assert event.resolved_at is not None
    assert [e.id for e in service.list_events(db, family.id)] == [event.id]


def test_only_what_is_older_than_the_asked_age_is_marked(db, family, gate):
    """«Всё, что старше двух дней» — про сорок восемь часов, а не про позавчера."""
    fresh = _alarm(db, family.id, gate, hours_ago=10)
    old = _alarm(db, family.id, gate, hours_ago=60)

    assert service.mark_seen(db, family.id, older_than_days=2) == 1

    db.refresh(fresh), db.refresh(old)
    assert fresh.resolution is None
    assert old.resolution == RESOLUTION_SEEN


def test_a_family_decision_does_not_get_overwritten(db, family, gate):
    """«Это свои» — разобранное событие; пачка его не трогает и не пересчитывает."""
    ours = _alarm(db, family.id, gate)
    service.mark_ours(db, family.id, ours.id)

    assert service.mark_seen(db, family.id) == 0
    db.refresh(ours)
    assert ours.resolution == RESOLUTION_OURS


def test_the_neighbours_alarms_are_not_yours_to_dismiss(db, family, gate, member):
    """Лента общая на семью, но не на сервер: чужая тревога остаётся непросмотренной."""
    from app.core.models import Family

    other = Family(name="Соседи")
    db.add(other)
    db.flush()
    their_camera = service.get_or_create_camera(db, other.id, "door", "Дверь")
    theirs = _alarm(db, other.id, their_camera)
    _alarm(db, family.id, gate)

    assert service.mark_seen(db, family.id) == 1

    db.refresh(theirs)
    assert theirs.resolution is None


def test_the_button_on_the_screen_marks_and_comes_back(as_member, db, family, gate):
    _alarm(db, family.id, gate)

    response = as_member.post("/security/events/seen", data={"older_than": 0, "only": "anomaly"},
                            follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/security/events?only=anomaly"
    assert service.unseen_count(db, family.id) == 0


def test_the_screen_offers_the_button_only_while_there_is_something_to_mark(as_member, db,
                                                                           family, gate):
    assert "Непросмотренных" not in as_member.get("/security/events").text

    _alarm(db, family.id, gate)

    assert "Непросмотренных" in as_member.get("/security/events").text


# --- уборка архива ----------------------------------------------------------

def test_old_records_go_away_and_fresh_ones_stay(db, family, gate):
    old = _stored(db, family.id, gate, "old.jpg", days_ago=9)
    fresh = _stored(db, family.id, gate, "fresh.jpg", days_ago=1)
    old_path = media.resolve(old.rel_path)

    result = retention.purge(db, family.id, older_than_days=7)

    assert result["records"] == 1
    assert not old_path.exists()
    assert [i.filename for i in db.query(MediaItem).all()] == [fresh.filename]


def test_cleaning_one_camera_leaves_the_others_alone(db, family, gate, yard):
    _stored(db, family.id, gate, "gate.jpg", days_ago=9)
    _stored(db, family.id, yard, "yard.jpg", days_ago=9)

    retention.purge(db, family.id, older_than_days=7, camera_id=gate.id)

    assert [i.filename for i in db.query(MediaItem).all()] == ["yard.jpg"]


def test_the_event_outlives_the_frame_the_family_asked_to_remove(db, family, gate):
    """То же правило, что у ротации: кадра нет, а что случилось — семья помнит."""
    captured = utc_now() - timedelta(days=9)
    path = media.media_dir("security", gate.slug, "2026-01-01") / "person.jpg"
    path.write_bytes(b"frame")
    service.record_event(db, family.id, gate, captured, detected_class="person",
                         confidence=0.9, snapshot_path=str(path))

    retention.purge(db, family.id, older_than_days=7)

    event = db.query(SecurityEvent).one()
    assert event.snapshot_path is None
    assert not path.exists()


def test_the_archive_of_another_family_is_not_touched(db, family, gate):
    from app.core.models import Family

    other = Family(name="Соседи")
    db.add(other)
    db.flush()
    their_camera = service.get_or_create_camera(db, other.id, "door", "Дверь")
    _stored(db, other.id, their_camera, "theirs.jpg", days_ago=9)
    _stored(db, family.id, gate, "ours.jpg", days_ago=9)

    retention.purge(db, family.id, older_than_days=7)

    assert [i.filename for i in db.query(MediaItem).all()] == ["theirs.jpg"]


def test_today_is_never_swept_away(db, family, gate):
    """У уборки нет пункта «всё»: срок меньше суток подтягивается до суток."""
    _stored(db, family.id, gate, "now.jpg", days_ago=0)

    assert retention.purge(db, family.id, older_than_days=0)["records"] == 0
    assert db.query(MediaItem).count() == 1


def test_the_button_on_the_archive_screen_cleans_and_says_what_it_did(as_member, db, family, gate):
    _stored(db, family.id, gate, "old.jpg", days_ago=9)

    response = as_member.post("/security/archive/purge",
                            data={"older_than": 7, "camera": "", "only": "all"},
                            follow_redirects=True)

    assert response.status_code == 200
    assert "Убрал 1 запись" in response.text
    assert db.query(MediaItem).count() == 0


# --- то же самое словами ----------------------------------------------------

def test_the_assistant_marks_events_seen_by_age(db, family, gate, member):
    fresh = _alarm(db, family.id, gate, hours_ago=10)
    _alarm(db, family.id, gate, hours_ago=60)

    result = tools.mark_events_seen(_ctx(db, member), older_than_days=2)

    assert result.ok and result.data == {"marked": 1, "unseen_left": 1}
    assert "старше 2 дней" in result.summary
    db.refresh(fresh)
    assert fresh.resolution is None


def test_the_assistant_says_plainly_when_there_was_nothing_to_mark(db, family, gate, member):
    result = tools.mark_events_seen(_ctx(db, member))

    assert result.ok and result.data["marked"] == 0
    assert "не было" in result.summary


def test_the_assistant_cleans_the_archive_of_one_camera_by_name(db, family, gate, yard, member):
    _stored(db, family.id, gate, "gate.jpg", days_ago=40)
    _stored(db, family.id, yard, "yard.jpg", days_ago=40)

    result = tools.clear_archive(_ctx(db, member), older_than_days=30, camera="калитка")

    assert result.ok and result.data["records"] == 1
    assert [i.filename for i in db.query(MediaItem).all()] == ["yard.jpg"]


def test_the_assistant_does_not_guess_which_camera_was_meant(db, family, gate, member):
    _stored(db, family.id, gate, "gate.jpg", days_ago=40)

    result = tools.clear_archive(_ctx(db, member), older_than_days=30, camera="гараж")

    assert not result.ok
    assert "Калитка" in result.summary
    assert db.query(MediaItem).count() == 1


def test_the_assistant_refuses_to_wipe_the_whole_archive(db, family, gate, member):
    """«Удали всё» — не срок. Инструмент не додумывает и не стирает сегодняшнее."""
    _stored(db, family.id, gate, "now.jpg", days_ago=0)

    result = tools.clear_archive(_ctx(db, member), older_than_days=0)

    assert not result.ok
    assert db.query(MediaItem).count() == 1


def test_the_count_on_the_screen_matches_the_feed_in_front_of_you(as_member, db, family, gate):
    """Лента показывает неделю — значит и «непросмотренных» считается за неделю."""
    _alarm(db, family.id, gate, hours_ago=24 * 30)
    _alarm(db, family.id, gate, hours_ago=2)

    assert "Непросмотренных — 1" in as_member.get("/security/events").text
    assert service.unseen_count(db, family.id) == 2      # но убрать можно и давнее


def test_the_offline_assistant_hears_the_phrase_as_said(db):
    """Без ключа модели чат разбирает слова сам — и «просмотрено» не должно теряться."""
    from app.agent import stub

    available = [{"function": {"name": name}}
                 for name in ("mark_events_seen", "clear_archive", "get_security_log")]

    def call(text):
        answer = stub.chat([{"role": "user", "content": text}], tools=available)
        return answer.tool_calls[0].name, answer.tool_calls[0].arguments

    assert call("пометь просмотренным всё старше двух дней") == (
        "mark_events_seen", {"older_than_days": 2})
    assert call("убери уведомления, я всё видел") == ("mark_events_seen", {"older_than_days": 0})
    assert call("почисти архив за месяц") == ("clear_archive", {"older_than_days": 30})
    # А просьба показать осталась просьбой показать.
    assert call("что было ночью дома?")[0] == "get_security_log"


def test_deleting_footage_always_asks_first(db):
    """Файлов потом не вернуть — значит, сам агент это делает только на максимуме."""
    from app.agent import registry

    assert registry.get("clear_archive").auto_from == 3
    assert registry.get("mark_events_seen").auto_from == 2
