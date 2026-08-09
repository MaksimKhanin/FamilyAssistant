"""Подписка браузера на уведомления и проверка связи."""
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import push
from app.core.auth import get_current_user
from app.core.db import get_db
from app.core.models import User

router = APIRouter(prefix="/push", tags=["push"])


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    """То, что отдаёт `PushSubscription.toJSON()` в браузере."""
    endpoint: str
    keys: SubscriptionKeys


@router.get("/key")
def vapid_key(current: User = Depends(get_current_user)):
    """Публичный ключ VAPID — его браузер передаёт в pushManager.subscribe()."""
    return {"key": push.public_key(), "configured": push.configured()}


@router.post("/subscribe")
def subscribe(
    payload: SubscriptionIn,
    user_agent: str = Header(default=""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    row = push.save_subscription(
        db, current.id, payload.endpoint, payload.keys.p256dh, payload.keys.auth,
        device_label=push.device_label_from(user_agent),
    )
    return {"status": "ok", "device": row.device_label, "devices": push.device_count(db, current.id)}


@router.post("/unsubscribe")
def unsubscribe(
    payload: dict,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    endpoint = (payload or {}).get("endpoint", "")
    removed = push.forget_subscription(db, endpoint) if endpoint else False
    return {"status": "ok" if removed else "not_found",
            "devices": push.device_count(db, current.id)}


@router.post("/test")
def test(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if not push.configured():
        return JSONResponse({"status": "not_configured",
                             "message": "VAPID-ключи не заданы на сервере"}, status_code=503)
    sent = push.send_test(db, current)
    return {"status": "ok" if sent else "no_devices", "sent": sent}
