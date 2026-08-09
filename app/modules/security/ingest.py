"""Ingest endpoint for the edge vision worker.

The worker at home does the cheap, constant part (RTSP, motion, YOLO) and only
uploads frames that already contain something. This endpoint stores the frame,
applies the household rules, and puts the result on the Event Bus — from there the
Telegram channel and the agent take over.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core import media
from app.core.config import settings
from app.core.db import get_db
from app.core.events import SECURITY_ANOMALY, SECURITY_EVENT_CREATED, bus
from app.core.logging import get_logger
from app.core.models import Family
from app.modules.security import service
from app.modules.security.models import VERDICT_NORMAL

router = APIRouter(prefix="/api/security", tags=["security-ingest"])
logger = get_logger("security.ingest")


def _check_api_key(authorization: str = Header(default="")):
    if not settings.ingest_api_key:
        raise HTTPException(status_code=503, detail="INGEST_API_KEY не задан на сервере")
    if authorization != f"Bearer {settings.ingest_api_key}":
        raise HTTPException(status_code=401, detail="Invalid API key")


def _resolve_family(db: Session, family_id: Optional[int]) -> int:
    if family_id:
        if db.get(Family, family_id) is None:
            raise HTTPException(status_code=404, detail=f"Семья {family_id} не найдена")
        return family_id
    families = db.query(Family).order_by(Family.id).limit(2).all()
    if not families:
        raise HTTPException(status_code=409, detail="Семья ещё не создана — пройдите онбординг")
    if len(families) > 1:
        raise HTTPException(status_code=400, detail="Укажите family_id: на сервере несколько семей")
    return families[0].id


@router.post("/events")
async def ingest_event(
    camera: str = Form(...),
    detected_class: str = Form(None),
    confidence: float = Form(None),
    area: int = Form(None),
    captured_at: str = Form(None),
    family_id: int = Form(None),
    snapshot: UploadFile = File(None),
    db: Session = Depends(get_db),
    _=Depends(_check_api_key),
):
    resolved_family = _resolve_family(db, family_id)
    camera_row = service.get_or_create_camera(db, resolved_family, camera)

    happened_at = _parse_time(captured_at)
    snapshot_path = None
    if snapshot is not None and snapshot.filename:
        data = await snapshot.read()
        if data:
            snapshot_path = media.store_bytes(
                data, "security", camera_row.slug, happened_at.strftime("%Y-%m-%d")
            )

    event = service.record_event(
        db, resolved_family, camera_row, happened_at,
        detected_class=detected_class, confidence=confidence, area=area,
        snapshot_path=snapshot_path,
    )

    payload = {"event_id": event.id, "family_id": resolved_family, "camera_id": camera_row.id,
               "verdict": event.verdict}
    bus.publish(SECURITY_EVENT_CREATED, payload)
    if event.verdict != VERDICT_NORMAL:
        bus.publish(SECURITY_ANOMALY, payload)

    logger.info(f"Событие с камеры {camera_row.slug}: {event.verdict} — {event.reason}")
    return {"status": "stored", "event_id": event.id, "verdict": event.verdict}


def _parse_time(raw: str) -> datetime:
    if not raw:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        logger.warning(f"Не разобрал время съёмки «{raw}», беру текущее")
        return datetime.utcnow()
