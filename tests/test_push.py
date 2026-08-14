"""Web push: шифрование, подписки и превращение события шины в уведомление."""
import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import push, webpush
from app.core.models import PushSubscription


@pytest.fixture
def browser():
    """Подписка «браузера»: пара ключей и общий секрет, как их отдаёт pushManager."""
    private = ec.generate_private_key(ec.SECP256R1())
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = os.urandom(16)
    subscription = webpush.Subscription(
        endpoint="https://push.example.com/subscription/abc",
        p256dh=webpush.b64e(public_raw),
        auth=webpush.b64e(auth),
    )
    return private, auth, subscription


def test_encrypted_message_can_be_read_only_with_the_browser_key(browser):
    private, auth, subscription = browser
    message = '{"title":"Дом","body":"Кто-то у калитки в 23:14"}'.encode("utf-8")

    body = webpush.encrypt(message, subscription)

    assert body[:16] != message[:16]                    # это точно не открытый текст
    assert webpush.decrypt(body, private, auth) == message


def test_each_message_is_encrypted_with_a_fresh_key(browser):
    _, _, subscription = browser
    first = webpush.encrypt(b"one", subscription)
    second = webpush.encrypt(b"one", subscription)
    assert first != second       # своя соль и эфемерный ключ на каждое сообщение


def test_wrong_key_cannot_decrypt(browser):
    _, auth, subscription = browser
    body = webpush.encrypt("секрет".encode("utf-8"), subscription)
    someone_else = ec.generate_private_key(ec.SECP256R1())

    with pytest.raises(Exception):
        webpush.decrypt(body, someone_else, auth)


def test_vapid_header_is_a_signed_jwt_for_the_push_service(browser):
    _, _, subscription = browser
    private, public = webpush.generate_vapid_keys()

    header = webpush.vapid_headers(subscription.endpoint, private, public, "mailto:a@b.c")
    token = header["Authorization"].split("t=")[1].split(",")[0]
    header_part, claims_part, signature = token.split(".")

    import json
    claims = json.loads(webpush.b64d(claims_part))
    assert claims["aud"] == "https://push.example.com"
    assert claims["sub"] == "mailto:a@b.c"
    assert len(webpush.b64d(signature)) == 64        # «сырая» подпись r||s, а не DER
    assert public in header["Authorization"]


# --- хранение подписок ----------------------------------------------------

def test_subscription_is_saved_per_device(db, member):
    push.save_subscription(db, member.id, "https://push/1", "key1", "auth1", "iPhone · Safari")
    push.save_subscription(db, member.id, "https://push/2", "key2", "auth2", "Android · Chrome")

    assert push.device_count(db, member.id) == 2


def test_resubscribing_the_same_device_updates_it(db, member):
    push.save_subscription(db, member.id, "https://push/1", "old", "auth", "iPhone · Safari")
    push.save_subscription(db, member.id, "https://push/1", "new", "auth", "iPhone · Safari")

    row = db.query(PushSubscription).one()
    assert row.p256dh == "new"
    assert push.device_count(db, member.id) == 1


def test_a_shared_device_moves_to_its_new_owner(db, member, other):
    push.save_subscription(db, member.id, "https://push/shared", "k", "a")
    push.save_subscription(db, other.id, "https://push/shared", "k", "a")

    assert push.device_count(db, member.id) == 0
    assert push.device_count(db, other.id) == 1


def test_forgetting_a_subscription(db, member):
    push.save_subscription(db, member.id, "https://push/1", "k", "a")
    assert push.forget_subscription(db, "https://push/1") is True
    assert push.forget_subscription(db, "https://push/1") is False


def test_device_label_is_recognisable():
    iphone = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
    assert push.device_label_from(iphone) == "iPhone · Safari"
    assert push.device_label_from("") is None


# --- доставка -------------------------------------------------------------

def test_dead_subscriptions_are_dropped_not_retried_forever(db, member, monkeypatch):
    push.save_subscription(db, member.id, "https://push/gone", "k", "a")
    push.save_subscription(db, member.id, "https://push/alive", "k", "a")
    monkeypatch.setattr(push.settings.push, "public_key", "pub")
    monkeypatch.setattr(push.settings.push, "private_key", "priv")

    def fake_send(subscription, *args, **kwargs):
        if subscription.endpoint.endswith("gone"):
            raise webpush.PushError("нет такой подписки", status=410, gone=True)
        return 201

    monkeypatch.setattr(push.webpush, "send", fake_send)

    delivered = push.send_to_users(db, [member.id], "Проверка")

    assert delivered == 1
    assert [row.endpoint for row in db.query(PushSubscription)] == ["https://push/alive"]


def test_temporary_failure_keeps_the_subscription(db, member, monkeypatch):
    push.save_subscription(db, member.id, "https://push/flaky", "k", "a")
    monkeypatch.setattr(push.settings.push, "public_key", "pub")
    monkeypatch.setattr(push.settings.push, "private_key", "priv")
    monkeypatch.setattr(push.webpush, "send",
                        lambda *a, **kw: (_ for _ in ()).throw(webpush.PushError("503", status=503)))

    assert push.send_to_users(db, [member.id], "Проверка") == 0
    assert push.device_count(db, member.id) == 1


def test_nothing_is_sent_without_vapid_keys(db, member, monkeypatch):
    push.save_subscription(db, member.id, "https://push/1", "k", "a")
    monkeypatch.setattr(push.settings.push, "public_key", "")
    monkeypatch.setattr(push.settings.push, "private_key", "")

    assert push.send_to_users(db, [member.id], "Проверка") == 0


def test_anomaly_on_the_bus_becomes_a_notification_with_a_link(db, member, monkeypatch):
    sent = {}
    monkeypatch.setattr(push, "send_to_users",
                        lambda db, user_ids, body, severity="info", url="/", tag=None:
                        sent.update(user_ids=user_ids, body=body, severity=severity, url=url) or 1)

    push.handle_agent_message({
        "user_ids": [member.id],
        "text": "Кто-то у калитки, 23:14",
        "severity": "alarm",
        "event_id": 7,
    })

    assert sent["severity"] == "alarm"
    assert sent["url"] == "/security/events?event_id=7"
    assert sent["body"] == "Кто-то у калитки, 23:14"


def test_empty_message_is_not_delivered(db, monkeypatch):
    called = []
    monkeypatch.setattr(push, "send_to_users", lambda *a, **kw: called.append(1))

    push.handle_agent_message({"user_ids": [], "text": "что-то"})
    push.handle_agent_message({"user_ids": [1], "text": "   "})

    assert called == []
