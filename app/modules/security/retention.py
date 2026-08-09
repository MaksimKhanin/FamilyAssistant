"""Media rotation.

Frames and clips are kept for `Camera.retention_days` (7–14 by default) and then
removed from disk. The event row survives — the family keeps the history of what
happened, just not the footage.
"""
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.modules.security.models import Camera, SecurityEvent

logger = get_logger("security.retention")


def rotate(db: Session) -> int:
    """Delete media older than each camera's retention window. Returns files removed."""
    removed = 0
    for camera in db.query(Camera).all():
        cutoff = datetime.utcnow() - timedelta(days=camera.retention_days)
        stale = (
            db.query(SecurityEvent)
            .filter(SecurityEvent.camera_id == camera.id,
                    SecurityEvent.happened_at < cutoff,
                    (SecurityEvent.snapshot_path.isnot(None)) | (SecurityEvent.clip_path.isnot(None)))
            .all()
        )
        for event in stale:
            for attribute in ("snapshot_path", "clip_path"):
                path = getattr(event, attribute)
                if not path:
                    continue
                try:
                    Path(path).unlink(missing_ok=True)
                    removed += 1
                except OSError as e:
                    logger.warning(f"Не удалось удалить {path}: {e}")
                setattr(event, attribute, None)
    if removed:
        db.commit()
        logger.info(f"Ротация архива: удалено файлов — {removed}")
    return removed
