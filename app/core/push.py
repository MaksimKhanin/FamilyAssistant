"""Доставка уведомлений на телефоны семьи.

Канал, заменивший мессенджер: панель установлена на телефон как приложение, и
сообщения агента — тревога с камеры, напоминание, утренняя сводка — приходят
push-уведомлением, не открывая её.

Модули об этом не знают: они публикуют AGENT_MESSAGE на шину, а здесь событие
превращается в уведомление. Ровно так же было устроено с ботом — сменился только
транспорт.
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core import webpush
from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.models import PushSubscription, User

logger = get_logger("push")

#: Как выглядит уведомление в зависимости от важности события.
SEVERITY = {
    "alarm": {"title": "Дом: стоит взглянуть", "urgency": "high", "require_interaction": True},
    "attention": {"title": "Ассистент", "urgency": "normal", "require_interaction": False},
    "info": {"title": "Ассистент", "urgency": "low", "require_interaction": False},
}


def public_key() -> str:
    return settings.push.public_key


def configured() -> bool:
    return settings.push.configured


# --- подписки -------------------------------------------------------------

def save_subscription(db: Session, user_id: int, endpoint: str, p256dh: str, auth: str,
                      device_label: str = None) -> PushSubscription:
    """Сохранить подписку. Один endpoint может смениться владельцем — общий телефон."""
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).one_or_none()
    if row is None:
        row = PushSubscription(endpoint=endpoint, user_id=user_id, p256dh=p256dh, auth=auth)
        db.add(row)
    else:
        row.user_id = user_id
        row.p256dh = p256dh
        row.auth = auth
    row.device_label = (device_label or row.device_label or "")[:128] or None
    db.commit()
    db.refresh(row)
    return row


def forget_subscription(db: Session, endpoint: str) -> bool:
    row = db.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).one_or_none()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def subscriptions_for(db: Session, user_ids: List[int]) -> List[PushSubscription]:
    if not user_ids:
        return []
    return db.query(PushSubscription).filter(PushSubscription.user_id.in_(user_ids)).all()


def device_count(db: Session, user_id: int) -> int:
    return db.query(PushSubscription).filter(PushSubscription.user_id == user_id).count()


def device_label_from(user_agent: str) -> Optional[str]:
    """Грубая, но узнаваемая подпись устройства — чтобы человек понял, что отключает."""
    if not user_agent:
        return None
    agent = user_agent.lower()
    platform = ("iPhone" if "iphone" in agent else
                "iPad" if "ipad" in agent else
                "Android" if "android" in agent else
                "Mac" if "macintosh" in agent else
                "Windows" if "windows" in agent else
                "Компьютер")
    browser = ("Chrome" if "chrome" in agent and "edg" not in agent else
               "Edge" if "edg" in agent else
               "Firefox" if "firefox" in agent else
               "Safari" if "safari" in agent else "браузер")
    return f"{platform} · {browser}"


# --- отправка -------------------------------------------------------------

def send_to_users(db: Session, user_ids: List[int], body: str, severity: str = "info",
                  url: str = "/", tag: str = None) -> int:
    """Разослать одно сообщение на все устройства перечисленных людей.

    Возвращает число доставленных уведомлений. Мёртвые подписки удаляются на месте:
    иначе список устройств семьи за год превращается в кладбище.
    """
    if not configured():
        logger.warning("VAPID-ключи не заданы — уведомления не отправляются")
        return 0

    style = SEVERITY.get(severity, SEVERITY["info"])
    payload = {
        "title": style["title"],
        "body": body,
        "url": url,
        "tag": tag or severity,
        "requireInteraction": style["require_interaction"],
    }

    delivered = 0
    dead = []
    for row in subscriptions_for(db, user_ids):
        subscription = webpush.Subscription(endpoint=row.endpoint, p256dh=row.p256dh, auth=row.auth)
        try:
            webpush.send(subscription, payload,
                         settings.push.private_key, settings.push.public_key,
                         settings.push.subject, urgency=style["urgency"])
        except webpush.PushError as e:
            if e.gone:
                dead.append(row)
            else:
                logger.warning(f"Не доставил уведомление на {row.device_label or 'устройство'}: {e}")
            continue
        row.last_used_at = datetime.utcnow()
        delivered += 1

    for row in dead:
        logger.info(f"Подписка устарела, удаляю: {row.device_label or row.endpoint[:40]}")
        db.delete(row)
    if delivered or dead:
        db.commit()

    return delivered


def handle_agent_message(payload: dict):
    """Обработчик шины: AGENT_MESSAGE → push.

    Подписывается только веб-процесс (см. app/main.py), иначе при работе через Redis
    одно событие превратилось бы в три уведомления — по одному на процесс.
    """
    user_ids = payload.get("user_ids") or []
    text = (payload.get("text") or "").strip()
    if not user_ids or not text:
        return

    url = "/"
    if payload.get("event_id"):
        url = f"/security/events?event_id={payload['event_id']}"

    with session_scope() as db:
        sent = send_to_users(db, user_ids, text, severity=payload.get("severity", "info"), url=url)
    if sent:
        logger.info(f"Отправлено уведомлений: {sent}")


def send_test(db: Session, user: User) -> int:
    return send_to_users(
        db, [user.id],
        f"{user.display_name}, проверка связи — уведомления работают.",
        severity="info", tag="test",
    )
