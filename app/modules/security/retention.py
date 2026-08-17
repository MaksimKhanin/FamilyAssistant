"""Media rotation.

Frames and clips are kept for `Camera.retention_days` (14 by default) and then
removed from disk. The event row survives — the family keeps the history of what
happened, just not the footage. Записи архива, наоборот, удаляются целиком: в них
нет ничего, кроме самого файла.

Диск здесь — расходный материал, и это единственное место, которое его освобождает.
Освобождает двумя способами, и разница между ними только в том, кто нажал:

    rotate — по сроку камеры, само, ночью, без спроса;
    purge  — по просьбе человека («убери всё старше двух дней»), сразу и с подтверждением.

Что именно исчезает, у них одинаково, поэтому сама работа живёт в общих `_drop_*`.
"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Set, Tuple

from sqlalchemy.orm import Session

from app.core import media
from app.core.clock import utc_now
from app.core.config import settings
from app.core.logging import get_logger
from app.modules.security.models import Camera, MediaItem, SecurityEvent

logger = get_logger("security.retention")

#: Верхняя граница «старше N суток» — не про политику хранения, а про то, что
#: `datetime - timedelta(days=N)` при огромном N валит `OverflowError` мимо
#: любой попытки её поймать понятным для человека сообщением. Десяти лет
#: заведомо хватает всему, что вообще может значить «убери самое старое».
MAX_OLDER_THAN_DAYS = 3650


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


def _drop_frames(events: Iterable[SecurityEvent]) -> int:
    """Кадр и клип события: файл убираем, строку — нет. Возвращает удалённые файлы."""
    removed = 0
    for event in events:
        for attribute in ("snapshot_path", "clip_path"):
            path = getattr(event, attribute)
            if not path:
                continue
            if _unlink(path):
                removed += 1
            setattr(event, attribute, None)
    return removed


def _drop_media(db: Session, items: List[MediaItem]) -> Tuple[int, int, Set[Path]]:
    """Записи архива: файл, превью и сама строка.

    Возвращает (удалено файлов, освобождено байт, каталоги, куда стоит заглянуть).
    """
    removed = freed = 0
    touched_dirs: Set[Path] = set()
    for item in items:
        for rel in (item.rel_path, item.thumb_rel_path):
            if not rel:
                continue
            path = media.resolve(rel)
            touched_dirs.add(path.parent)
            if _unlink(str(path)):
                removed += 1
        freed += item.size_bytes or 0
        db.delete(item)
    return removed, freed, touched_dirs


def _prune(touched_dirs: Set[Path]) -> None:
    """Каталоги нарезаны по дням, так что после уборки их остаётся много и все пустые."""
    root = Path(settings.media_root).resolve()
    for directory in touched_dirs:
        try:
            media.prune_empty_dirs(*directory.resolve().relative_to(root).parts)
        except (ValueError, OSError):
            continue


def _stale_events(db: Session, cutoff: datetime, *, camera_id: int = None,
                  family_id: int = None):
    query = db.query(SecurityEvent).filter(
        SecurityEvent.happened_at < cutoff,
        (SecurityEvent.snapshot_path.isnot(None)) | (SecurityEvent.clip_path.isnot(None)),
    )
    if camera_id:
        query = query.filter(SecurityEvent.camera_id == camera_id)
    if family_id:
        query = query.filter(SecurityEvent.family_id == family_id)
    return query


def _stale_media(db: Session, cutoff: datetime, *, camera_id: int = None, family_id: int = None):
    query = db.query(MediaItem).filter(MediaItem.captured_at < cutoff)
    if camera_id:
        query = query.filter(MediaItem.camera_id == camera_id)
    if family_id:
        query = query.filter(MediaItem.family_id == family_id)
    return query


def rotate(db: Session) -> int:
    """Delete media older than each camera's retention window. Returns files removed."""
    removed = dropped_rows = 0
    touched_dirs: Set[Path] = set()

    for camera in db.query(Camera).all():
        cutoff = datetime.utcnow() - timedelta(days=_retention_days(camera))
        stale_events = _stale_events(db, cutoff, camera_id=camera.id).all()
        stale_media = _stale_media(db, cutoff, camera_id=camera.id).all()
        removed += _drop_frames(stale_events)
        files, _, dirs = _drop_media(db, stale_media)
        removed += files
        dropped_rows += len(stale_media) + len(stale_events)
        touched_dirs |= dirs

    if dropped_rows:
        db.commit()
        _prune(touched_dirs)
        logger.info(f"Ротация архива: удалено файлов — {removed}")
    return removed


def purge(db: Session, family_id: int, older_than_days: int, camera_id: int = None) -> dict:
    """Убрать записи архива старше `older_than_days` — по просьбе, а не по сроку.

    Ротация освобождает диск сама, но по своему календарю: она ждёт, пока
    истечёт срок камеры. Здесь человек говорит «убери всё старше двух дней» и
    получает это сразу — по всему архиву или по одной камере.

    Событие остаётся в ленте и без кадра, ровно как после ротации: что случилось,
    семья помнит дольше, чем хранится запись.

    Возвращает {"records": сколько записей убрано, "bytes": сколько освободилось}.
    """
    older_than_days = max(1, min(older_than_days, MAX_OLDER_THAN_DAYS))
    cutoff = utc_now() - timedelta(days=older_than_days)

    _drop_frames(_stale_events(db, cutoff, camera_id=camera_id, family_id=family_id).all())
    items = _stale_media(db, cutoff, camera_id=camera_id, family_id=family_id).all()
    _, freed, touched_dirs = _drop_media(db, items)

    db.commit()
    _prune(touched_dirs)
    if items:
        logger.info(f"Архив почищен по просьбе: записей — {len(items)}, освобождено {freed} б")
    return {"records": len(items), "bytes": freed}
