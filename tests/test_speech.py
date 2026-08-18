"""Озвучка: чем читать — на семью, читать ли — каждому своё (app/core/speech.py).

Проверяется именно этот раздел ответственности: администратор задаёт голос всему
дому, участник включает звук себе, и одно без другого не звучит. Плюс поведение,
ради которого озвучка вообще держится честной, — модель, которой нет в окружении,
не молчит, а уступает голосу устройства.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import family as family_service
from app.core import speech
from app.core.config import settings as app_settings
from app.core.db import get_db
from app.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _login(client, user):
    client.post("/login", data={"username": user.username, "password": "pw"},
                follow_redirects=False)
    return client


@pytest.fixture
def voiced(monkeypatch):
    """Модель озвучки названа в окружении и отвечает звуком."""
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "body": json, "headers": headers})
        return httpx.Response(200, content=b"ID3-audio", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(app_settings.speech, "base_url", "http://voice.local/v1")
    monkeypatch.setattr(app_settings.speech, "api_key", "k")
    monkeypatch.setattr(app_settings.speech, "model", "tts-1")
    return sent


# --- кто чем распоряжается -------------------------------------------------

def test_the_administrator_chooses_the_voice_for_the_whole_family(client, db, admin, member):
    _login(client, admin)

    client.post("/settings/model", data={"speech_mode": "model", "speech_voice": "nova",
                                         "speech_rate": "120"}, follow_redirects=False)

    settings_row = family_service.get_settings(db, admin.family_id)
    assert settings_row.speech_mode == "model"
    assert settings_row.speech_voice == "nova"
    assert settings_row.speech_rate == 120
    # Выбор семьи — это ещё не звук: человеку он ничего не включил.
    db.refresh(member)
    assert member.speech_enabled is False


def test_a_member_switches_the_reading_on_for_themselves(client, db, member):
    _login(client, member)

    client.post("/settings/profile/speech", data={"enabled": "on"}, follow_redirects=False)
    db.refresh(member)
    assert member.speech_enabled is True

    client.post("/settings/profile/speech", data={"enabled": "off"}, follow_redirects=False)
    db.refresh(member)
    assert member.speech_enabled is False


def test_one_member_does_not_switch_it_on_for_another(client, db, member, other):
    """Тумблер личный: сосед по семье слушает или не слушает сам."""
    _login(client, member)

    client.post("/settings/profile/speech", data={"enabled": "on"}, follow_redirects=False)

    db.refresh(other)
    assert other.speech_enabled is False


def test_the_speech_settings_are_the_administrators_screen(client, db, member):
    """Голос семьи с участникового экрана не меняется — адрес админский."""
    _login(client, member)

    response = client.post("/settings/model", data={"speech_mode": "model"},
                           follow_redirects=False)

    assert response.status_code == 403
    assert family_service.get_settings(db, member.family_id).speech_mode == "device"


def test_both_screens_show_their_half_of_the_setting(client, db, admin, member):
    """Голос — на админском экране, тумблер — в профиле, и не наоборот."""
    _login(client, admin)
    model_screen = client.get("/settings/model").text
    assert "Озвучка ответов" in model_screen
    assert 'name="speech_mode"' in model_screen and 'name="speech_voice"' in model_screen
    # У администратора ассистента нет — читать ему нечего и нечем (ADR-0008).
    assert "/settings/profile/speech" not in client.get("/settings/profile").text

    _login(client, member)
    profile = client.get("/settings/profile").text
    assert "Озвучивать ответы" in profile
    assert "/settings/profile/speech" in profile
    # Голос выбирает администратор — на участниковом экране его только видно.
    assert 'name="speech_voice"' not in profile


# --- что об этом знает браузер ---------------------------------------------

def test_the_panel_says_nothing_about_speech_until_it_is_switched_on(client, db, member):
    _login(client, member)

    assert 'id="speech-settings"' not in client.get("/chat").text


def test_the_panel_tells_the_browser_how_to_read(client, db, member, voiced):
    family_service.get_settings(db, member.family_id).speech_mode = "model"
    member.speech_enabled = True
    db.commit()
    _login(client, member)

    markup = client.get("/chat").text

    assert 'id="speech-settings"' in markup
    assert 'data-mode="model"' in markup


def test_a_model_that_is_not_configured_leaves_the_device_reading(client, db, member):
    """Пустой SPEECH_MODEL не должен оборачиваться тишиной вместо ответа."""
    family_service.get_settings(db, member.family_id).speech_mode = "model"
    member.speech_enabled = True
    db.commit()
    _login(client, member)

    assert 'data-mode="device"' in client.get("/chat").text


# --- звук ------------------------------------------------------------------

def test_the_model_reads_with_the_family_voice(client, db, member, voiced):
    settings_row = family_service.get_settings(db, member.family_id)
    settings_row.speech_mode = "model"
    settings_row.speech_voice = "nova"
    settings_row.speech_rate = 120
    member.speech_enabled = True
    db.commit()
    _login(client, member)

    response = client.post("/chat/speech", data={"text": "Записал ужин."})

    assert response.status_code == 200
    assert response.content == b"ID3-audio"
    assert voiced[0]["url"] == "http://voice.local/v1/audio/speech"
    assert voiced[0]["body"]["model"] == "tts-1"
    assert voiced[0]["body"]["voice"] == "nova"
    assert voiced[0]["body"]["input"] == "Записал ужин."
    assert voiced[0]["body"]["speed"] == 1.2


def test_nobody_is_read_aloud_without_their_own_switch(client, db, member, voiced):
    """Тумблер выключен — сервер за звуком не ходит, даже если его попросили."""
    family_service.get_settings(db, member.family_id).speech_mode = "model"
    db.commit()
    _login(client, member)

    response = client.post("/chat/speech", data={"text": "Записал ужин."})

    assert response.status_code == 409
    assert voiced == []


def test_the_device_voice_needs_no_server(client, db, member, voiced):
    """Семья читает устройством — в модель за это никто не ходит и не платит."""
    member.speech_enabled = True
    db.commit()
    _login(client, member)

    response = client.post("/chat/speech", data={"text": "Записал ужин."})

    assert response.status_code == 409
    assert voiced == []


def test_a_broken_model_hands_the_reading_back_to_the_device(client, db, member, monkeypatch):
    """Провайдер ответил ошибкой — панель получает отказ, а не пятисотку."""
    def failing_post(url, json=None, headers=None, timeout=None):
        return httpx.Response(500, text="upstream down", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", failing_post)
    monkeypatch.setattr(app_settings.speech, "base_url", "http://voice.local/v1")
    monkeypatch.setattr(app_settings.speech, "model", "tts-1")
    settings_row = family_service.get_settings(db, member.family_id)
    settings_row.speech_mode = "model"
    member.speech_enabled = True
    db.commit()
    _login(client, member)

    assert client.post("/chat/speech", data={"text": "Записал ужин."}).status_code == 409


def test_long_answers_are_cut_before_they_are_paid_for(voiced):
    speech.synthesize("а" * 5000)

    assert len(voiced[0]["body"]["input"]) == speech.TEXT_LIMIT


def test_the_speed_stays_within_what_can_be_understood():
    assert speech.normalize_rate(300) == speech.RATE_MAX
    assert speech.normalize_rate(10) == speech.RATE_MIN
    assert speech.normalize_rate("не число") == speech.DEFAULT_RATE
    assert speech.normalize_rate(115) == 110
