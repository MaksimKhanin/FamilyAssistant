"""Web Push: шифрование сообщения и подпись VAPID.

Реализовано по спецификациям напрямую (RFC 8291 — шифрование, RFC 8188 —
кодирование aes128gcm, RFC 8292 — VAPID), на одном `cryptography`. Готовая
библиотека `pywebpush` тянет за собой заброшенный `http-ece`, который не
собирается на свежем setuptools; спецификация же стабильна и укладывается в
сотню строк, которые можно проверить тестом на расшифровку.

Здесь только криптография и отправка. Кому слать и по какому поводу — в
`app/core/push.py`.
"""
import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.logging import get_logger

logger = get_logger("webpush")

RECORD_SIZE = 4096
DEFAULT_TTL = 12 * 60 * 60          # столько push-сервис хранит недоставленное
VAPID_LIFETIME = 12 * 60 * 60       # больше 24 часов спецификация запрещает


class PushError(RuntimeError):
    """Отправка не удалась. `gone=True` — подписки больше нет, её надо забыть."""

    def __init__(self, message: str, status: int = 0, gone: bool = False):
        super().__init__(message)
        self.status = status
        self.gone = gone


# --- base64url без выравнивания ------------------------------------------

def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


# --- ключи VAPID ----------------------------------------------------------

def generate_vapid_keys() -> Tuple[str, str]:
    """Новая пара ключей: (приватный, публичный) в base64url. См. scripts/vapid_keys.py."""
    private = ec.generate_private_key(ec.SECP256R1())
    private_raw = private.private_numbers().private_value.to_bytes(32, "big")
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64e(private_raw), b64e(public_raw)


def load_vapid_private(private_b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64d(private_b64), "big"), ec.SECP256R1())


@dataclass
class Subscription:
    """То, что браузер отдал при подписке (`PushSubscription.toJSON()`)."""
    endpoint: str
    p256dh: str      # публичный ключ браузера, base64url, 65 байт
    auth: str        # общий секрет, base64url, 16 байт


# --- RFC 8291: шифрование полезной нагрузки -------------------------------

def encrypt(payload: bytes, subscription: Subscription) -> bytes:
    """Зашифровать сообщение ключами конкретной подписки (content-encoding aes128gcm)."""
    ua_public_raw = b64d(subscription.p256dh)
    auth_secret = b64d(subscription.auth)

    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_raw)
    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public_raw = as_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    shared = as_private.exchange(ec.ECDH(), ua_public)

    # Из общего секрета и auth_secret получаем IKM, привязанный к обеим сторонам.
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public_raw + as_public_raw,
    ).derive(shared)

    salt = os.urandom(16)
    content_key = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
                       info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)

    # 0x02 — признак последней (и единственной) записи.
    ciphertext = AESGCM(content_key).encrypt(nonce, payload + b"\x02", None)

    header = (salt
              + RECORD_SIZE.to_bytes(4, "big")
              + len(as_public_raw).to_bytes(1, "big")
              + as_public_raw)
    return header + ciphertext


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """Обратная операция — существует ради теста: шифруем и тут же расшифровываем."""
    salt, body = body[:16], body[16:]
    body = body[4:]                                   # record size, здесь не нужен
    key_length, body = body[0], body[1:]
    as_public_raw, ciphertext = body[:key_length], body[key_length:]

    ua_public_raw = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public_raw)
    )
    ikm = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=auth_secret,
        info=b"WebPush: info\x00" + ua_public_raw + as_public_raw,
    ).derive(shared)

    content_key = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
                       info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(ikm)

    return AESGCM(content_key).decrypt(nonce, ciphertext, None).rstrip(b"\x02")


# --- RFC 8292: подпись VAPID ----------------------------------------------

def vapid_headers(endpoint: str, private_b64: str, public_b64: str, subject: str) -> dict:
    """Заголовок Authorization, которым сервер доказывает push-сервису, кто он."""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    audience = f"{parsed.scheme}://{parsed.netloc}"

    header = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64e(json.dumps(
        {"aud": audience, "exp": int(time.time()) + VAPID_LIFETIME, "sub": subject},
        separators=(",", ":"),
    ).encode())
    signing_input = f"{header}.{claims}".encode("ascii")

    der = load_vapid_private(private_b64).sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = asym_utils.decode_dss_signature(der)
    # JWS ждёт «сырую» подпись r||s, а cryptography отдаёт DER.
    signature = b64e(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    return {"Authorization": f"vapid t={header}.{claims}.{signature}, k={public_b64}"}


# --- отправка -------------------------------------------------------------

def send(subscription: Subscription, payload: dict, private_b64: str, public_b64: str,
         subject: str, ttl: int = DEFAULT_TTL, urgency: str = "normal",
         timeout: float = 10.0) -> int:
    """Отправить одно уведомление. Бросает PushError, если не вышло."""
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"), subscription)

    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
        **vapid_headers(subscription.endpoint, private_b64, public_b64, subject),
    }

    try:
        response = httpx.post(subscription.endpoint, content=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise PushError(f"Push-сервис недоступен: {e}") from e

    if response.status_code in (404, 410):
        # Подписка мертва: браузер её отозвал или переустановили приложение.
        raise PushError("Подписка больше не действует", status=response.status_code, gone=True)
    if response.status_code >= 400:
        raise PushError(f"Push-сервис ответил {response.status_code}: {response.text[:200]}",
                        status=response.status_code)
    return response.status_code
