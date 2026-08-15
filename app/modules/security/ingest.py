"""Ingest endpoint — единственная дверь, в которую стучится домашний рекордер.

Рекордер дома делает дешёвую постоянную работу (RTSP, движение, YOLO, нарезка
видео) и присылает сюда готовые файлы: штатные чанки записи и снимки
срабатываний. Здесь файл кладётся в архив, а если в нём кто-то распознан —
его просеивает домашнее сито, и результат уходит на шину событий; дальше
подхватывают Telegram-канал и агент.

Протокол намеренно оставлен таким, каким его говорит рекордер (multipart с
`file`/`camera`/`filename`, метаданные съёмки — в имени файла), чтобы домашняя
часть системы не требовала переустановки при переезде сервера.
"""
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core import media
from app.core.clock import to_local, to_utc, utc_now
from app.core.config import settings
from app.core.db import get_db
from app.core.events import SECURITY_ANOMALY, SECURITY_EVENT_CREATED, bus
from app.core.logging import get_logger
from app.core.models import Family
from app.modules.security import filenames, service, thumbnails
from app.modules.security.models import VERDICT_NORMAL

router = APIRouter(prefix="/api/security", tags=["security-ingest"])
logger = get_logger("security.ingest")

CHUNK_SIZE = 1024 * 1024
#: Насколько далеко от начала чанка искать событие, к которому он относится.
CLIP_WINDOW = timedelta(minutes=5)


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


async def _stream_to_disk(upload: UploadFile, dest: Path) -> int:
    """Записать загрузку на диск по кускам — файл видео может быть большим."""
    size = 0
    with open(dest, "wb") as out:
        while chunk := await upload.read(CHUNK_SIZE):
            out.write(chunk)
            size += len(chunk)
    return size


@router.post("/media")
async def ingest_media(
    camera: str = Form(...),
    filename: str = Form(...),
    is_alert: bool = Form(False),
    is_merged: bool = Form(False),
    detected_class: str = Form(None),
    confidence: float = Form(None),
    area: int = Form(None),
    family_id: int = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _=Depends(_check_api_key),
):
    try:
        name = filenames.safe_filename(filename)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    resolved_family = _resolve_family(db, family_id)
    camera_row = service.get_or_create_camera(db, resolved_family, camera)

    existing = service.find_media(db, camera_row.id, name)
    if existing is not None:
        return {"status": "duplicate", "id": existing.id}

    # Время в имени — локальное время дома; в базе всё живёт в UTC.
    local_captured = filenames.parse_captured_at(name)
    captured_at = to_utc(local_captured) if local_captured else utc_now()
    kind = filenames.guess_kind(name)

    day = to_local(captured_at).strftime("%Y-%m-%d")
    dest = media.media_dir("security", camera_row.slug, day) / name
    size = await _stream_to_disk(file, dest)
    if not size:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Пустой файл")

    # cv2 декодирует файл синхронно — в event loop такому не место.
    thumb = await run_in_threadpool(thumbnails.generate, dest, kind)

    # Распознанный объект — повод пропустить кадр через сито и, возможно, разбудить семью.
    # Штатный чанк видео проходит молча: он нужен архиву, а не ленте.
    event = None
    if detected_class:
        event = service.record_event(
            db, resolved_family, camera_row, captured_at,
            detected_class=detected_class, confidence=confidence, area=area,
            snapshot_path=str(dest),
        )
    elif is_alert and kind == filenames.KIND_VIDEO:
        event = service.attach_clip(db, camera_row.id, captured_at, str(dest), CLIP_WINDOW)

    item = service.record_media(
        db, family_id=resolved_family, camera=camera_row, filename=name, kind=kind,
        rel_path=media.relative(dest),
        thumb_rel_path=media.relative(thumb) if thumb else None,
        captured_at=captured_at, size_bytes=size,
        is_alert=is_alert or bool(detected_class), is_merged=is_merged,
        detected_class=detected_class, confidence=confidence, area=area,
        event_id=event.id if event is not None else None,
    )

    if event is not None and detected_class:
        service.adopt_pending_clip(db, event, CLIP_WINDOW)

        payload = {"event_id": event.id, "family_id": resolved_family,
                   "camera_id": camera_row.id, "verdict": event.verdict}
        bus.publish(SECURITY_EVENT_CREATED, payload)
        if event.verdict != VERDICT_NORMAL:
            bus.publish(SECURITY_ANOMALY, payload)
        logger.info(f"Событие с камеры {camera_row.slug}: {event.verdict} — {event.reason}")
    else:
        logger.debug(f"В архив с камеры {camera_row.slug}: {name}")

    return {
        "status": "stored",
        "id": item.id,
        "kind": kind,
        "verdict": event.verdict if event is not None else None,
    }
