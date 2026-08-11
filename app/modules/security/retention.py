"""Media rotation.

Frames and clips are kept for `Camera.retention_days` (14 by default) and then
removed from disk. The event row survives — the family keeps the history of what
happened, just not the footage. Записи архива, наоборот, удаляются целиком: в них
нет ничего, кроме самого файла.

Диск здесь — расходный материал, и это единственное место, которое его освобождает.
"""
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core import media
from app.core.config import settings
from app.core.logging import get_logger
from app.modules.security.models import Camera, MediaItem, SecurityEvent

logger = get_logger("security.retention")


def _unlink(path: str) -> bool:
    if not path:
        return False
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError as e:
        logger.warning(f"Не удалось удалить {path}: {e}")
        return False


def _retention_days(camera: Camera) -> int:
    """Срок хранения камеры, а если он не задан — общий по установке."""
    return camera.retention_days or settings.media_retention_days


def rotate(db: Session) -> int:
    """Delete media older than each camera's retention window. Returns files removed."""
    removed = 0
    touched_dirs = set()

    for camera in db.query(Camera).all():
        cutoff = datetime.utcnow() - timedelta(days=_retention_days(camera))

        # 1. Кадры и клипы, на которые ссылаются события: файл убираем, строку — нет.
        stale_events = (
            db.query(SecurityEvent)
            .filter(SecurityEvent.camera_id == camera.id,
                    SecurityEvent.happened_at < cutoff,
                    (SecurityEvent.snapshot_path.isnot(None)) | (SecurityEvent.clip_path.isnot(None)))
            .all()
        )
        for event in stale_events:
            for attribute in ("snapshot_path", "clip_path"):
                path = getattr(event, attribute)
                if not path:
                    continue
                if _unlink(path):
                    removed += 1
                setattr(event, attribute, None)

        # 2. Записи архива: файл, превью и сама строка.
        stale_media = (
            db.query(MediaItem)
            .filter(MediaItem.camera_id == camera.id, MediaItem.captured_at < cutoff)
            .all()
        )
        for item in stale_media:
            for rel in (item.rel_path, item.thumb_rel_path):
                if not rel:
                    continue
                path = media.resolve(rel)
                touched_dirs.add(path.parent)
                if _unlink(str(path)):
                    removed += 1
            db.delete(item)

    if removed:
        db.commit()
        # Каталоги нарезаны по дням, так что после ротации их остаётся много и все пустые.
        for directory in touched_dirs:
            try:
                media.prune_empty_dirs(*directory.resolve().relative_to(
                    Path(settings.media_root).resolve()).parts)
            except (ValueError, OSError):
                continue
        logger.info(f"Ротация архива: удалено файлов — {removed}")
    return removed
