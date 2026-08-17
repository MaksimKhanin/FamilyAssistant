"""Security domain logic — and, above all, the filter.

The point of this module is not analytics. It is that nobody in the family should
ever have to scroll through camera footage: they see a notification when something
is genuinely worth a look, and everything else quietly lands in a log.

The filter is a cascade, cheapest first:

    YOLO дома     →  человек/машина в кадре вообще есть?   (иначе кадр не доедет сюда)
    сито          →  время, зона, класс объекта             (детерминированно, мгновенно)
    модель        →  только для спорных случаев             (classify_event, по желанию)

`decide` is the sieve step and is pure — no database, no clock, no I/O — so the
household's «что считать тревогой» is one readable function that can be tested.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import media as media_module
from app.core.clock import days_ago_start_utc, to_local, utc_now
from app.core.logging import get_logger
from app.modules.security import retention
from app.modules.security.models import (
    RESOLUTION_OURS, RESOLUTION_SEEN, VERDICT_ANOMALY, VERDICT_CHECK, VERDICT_NORMAL,
    Camera, MediaItem, SecurityEvent,
)

logger = get_logger("security")

#: Сколько файлов на странице архива. Держим кратным сетке, чтобы последний ряд не рвался.
PAGE_SIZE = 24

#: Глубина ленты событий. Столько же считает и «непросмотренных» на экране: цифра
#: должна сходиться с тем, что человек видит перед собой.
FEED_DAYS = 7

#: Периоды, которыми человек мыслит, когда наводит порядок: «просмотрено всё»,
#: «просмотрено всё, что старше двух дней». Число — возраст события в сутках.
SEEN_PERIODS = [(0, "всё"), (1, "старше суток"), (2, "старше двух дней"), (7, "старше недели")]
#: То же для архива, но без «всего»: файлы удаляются насовсем, и пункта
#: «стереть весь архив одной кнопкой» в панели быть не должно.
PURGE_PERIODS = [(1, "старше суток"), (2, "старше двух дней"),
                 (7, "старше недели"), (30, "старше месяца")]

#: Классы, из-за которых вообще имеет смысл кого-то беспокоить.
NOTABLE_CLASSES = ("person",)
#: Классы, которые интересны только внутри дома или ночью.
VEHICLE_CLASSES = ("car", "motorcycle", "truck", "bus")


@dataclass(frozen=True)
class Decision:
    verdict: str
    reason: str


def _is_quiet_hour(hour: int, quiet_from: int, quiet_to: int) -> bool:
    """Quiet window may wrap midnight (23 → 6)."""
    if quiet_from == quiet_to:
        return False
    if quiet_from < quiet_to:
        return quiet_from <= hour < quiet_to
    return hour >= quiet_from or hour < quiet_to


def decide(camera: Camera, detected_class: str, happened_at: datetime,
           confidence: float = None) -> Decision:
    """Rules step: what to make of one detection. Pure function."""
    detected_class = (detected_class or "").lower()
    quiet = _is_quiet_hour(happened_at.hour, camera.quiet_from, camera.quiet_to)

    if detected_class not in NOTABLE_CLASSES + VEHICLE_CLASSES:
        return Decision(VERDICT_NORMAL, "Ничего, на что стоит смотреть")

    if confidence is not None and confidence < 0.35:
        return Decision(VERDICT_NORMAL, "Слишком неуверенное распознавание")

    if detected_class in VEHICLE_CLASSES:
        if quiet:
            return Decision(VERDICT_CHECK, f"Машина у камеры «{camera.label}» в необычное время")
        return Decision(VERDICT_NORMAL, "Машина в обычное время")

    # дальше — только человек
    if camera.always_notify:
        return Decision(VERDICT_ANOMALY, f"Человек в зоне «{camera.zone}», где обычно никого нет")
    if quiet:
        return Decision(VERDICT_ANOMALY, f"Человек у камеры «{camera.label}» вне обычного времени")
    return Decision(VERDICT_NORMAL, "Обычная дневная жизнь дома")


# --- cameras --------------------------------------------------------------

def get_or_create_camera(db: Session, family_id: int, slug: str, label: str = None) -> Camera:
    camera = (
        db.query(Camera)
        .filter(Camera.family_id == family_id, Camera.slug == slug)
        .one_or_none()
    )
    if camera is None:
        camera = Camera(family_id=family_id, slug=slug, label=label or slug.replace("_", " ").capitalize())
        db.add(camera)
        db.commit()
        db.refresh(camera)
        logger.info(f"Зарегистрирована новая камера: {slug}")
    return camera


def list_cameras(db: Session, family_id: int) -> List[Camera]:
    return db.query(Camera).filter(Camera.family_id == family_id).order_by(Camera.label).all()


def set_camera_notify(db: Session, family_id: int, camera_id: int, enabled: bool) -> Optional[Camera]:
    camera = db.get(Camera, camera_id)
    if camera is None or camera.family_id != family_id:
        return None
    camera.notify_enabled = enabled
    db.commit()
    return camera


# --- events ---------------------------------------------------------------

def record_event(db: Session, family_id: int, camera: Camera, happened_at: datetime,
                 detected_class: str = None, confidence: float = None, area: int = None,
                 snapshot_path: str = None, clip_path: str = None) -> SecurityEvent:
    # Правила смотрят на «тихие часы» дома, поэтому решение принимается по
    # локальному времени, а храним всё по-прежнему в UTC.
    decision = decide(camera, detected_class, to_local(happened_at), confidence)

    event = SecurityEvent(
        family_id=family_id,
        camera_id=camera.id,
        happened_at=happened_at,
        verdict=decision.verdict,
        reason=decision.reason,
        detected_class=detected_class,
        confidence=confidence,
        area=area,
        snapshot_path=snapshot_path,
        clip_path=clip_path,
    )
    camera.last_seen_at = happened_at
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def list_events(db: Session, family_id: int, only: str = "all", days: int = FEED_DAYS,
                limit: int = 60) -> List[SecurityEvent]:
    query = (
        db.query(SecurityEvent)
        .filter(SecurityEvent.family_id == family_id,
                SecurityEvent.happened_at >= days_ago_start_utc(days - 1))
    )
    if only == "anomaly":
        query = query.filter(SecurityEvent.verdict.in_((VERDICT_ANOMALY, VERDICT_CHECK)))
    elif only == "normal":
        query = query.filter(SecurityEvent.verdict == VERDICT_NORMAL)
    return query.order_by(SecurityEvent.happened_at.desc()).limit(limit).all()


def get_event(db: Session, family_id: int, event_id: int) -> Optional[SecurityEvent]:
    event = db.get(SecurityEvent, event_id)
    return event if event is not None and event.family_id == family_id else None


def anomaly_count(db: Session, family_id: int, days: int = 1) -> int:
    return (
        db.query(SecurityEvent)
        .filter(SecurityEvent.family_id == family_id,
                SecurityEvent.verdict.in_((VERDICT_ANOMALY, VERDICT_CHECK)),
                SecurityEvent.resolution.is_(None),
                SecurityEvent.happened_at >= days_ago_start_utc(days - 1))
        .count()
    )


def _unseen(db: Session, family_id: int, older_than_days: int = 0, within_days: int = None):
    """Тревоги, которые никто ещё не разобрал; `older_than_days` отсекает свежие.

    Возраст здесь скользящий, а не календарный: «старше двух дней» человек
    произносит про сорок восемь часов, а не про позавчерашнюю полночь.
    """
    query = (
        db.query(SecurityEvent)
        .filter(SecurityEvent.family_id == family_id,
                SecurityEvent.verdict.in_((VERDICT_ANOMALY, VERDICT_CHECK)),
                SecurityEvent.resolution.is_(None))
    )
    if older_than_days > 0:
        # Верхняя граница та же, что у retention.purge, и по той же причине:
        # без неё огромное N валит `datetime - timedelta` в OverflowError.
        capped = min(older_than_days, retention.MAX_OLDER_THAN_DAYS)
        query = query.filter(SecurityEvent.happened_at < utc_now() - timedelta(days=capped))
    if within_days:
        query = query.filter(SecurityEvent.happened_at >= days_ago_start_utc(within_days - 1))
    return query


def unseen_count(db: Session, family_id: int, older_than_days: int = 0,
                 within_days: int = None) -> int:
    """Сколько тревог висит неразобранными. `within_days` ограничивает глубиной ленты."""
    return _unseen(db, family_id, older_than_days, within_days).count()


def mark_seen(db: Session, family_id: int, older_than_days: int = 0) -> int:
    """«Посмотрел, вопросов нет» — сразу по всей ленте. Возвращает, скольких коснулось.

    Разбирать тревоги по одной семья не станет: к вечеру их десяток, и значок
    в навигации горит не потому, что дома что-то не так, а потому, что до него
    не дошли руки. Записи при этом остаются на месте — гаснет только «непросмотренное».
    """
    changed = _unseen(db, family_id, older_than_days).update(
        {SecurityEvent.resolution: RESOLUTION_SEEN, SecurityEvent.resolved_at: utc_now()},
        synchronize_session=False,
    )
    if changed:
        db.commit()
    return changed


def mark_ours(db: Session, family_id: int, event_id: int) -> Optional[SecurityEvent]:
    """«Это свои, всё хорошо» — снимает тревогу, не удаляя запись."""
    event = get_event(db, family_id, event_id)
    if event is None:
        return None
    event.resolution = RESOLUTION_OURS
    event.resolved_at = utc_now()
    db.commit()
    return event


def attach_clip(db: Session, camera_id: int, captured_at: datetime, clip_path: str,
                window: timedelta) -> Optional[SecurityEvent]:
    """Подклеить видео-чанк к событию, из-за которого он был помечен тревожным.

    Рекордер помечает весь чанк, внутри которого сработала детекция, и присылает
    его отдельно от снимка — так что связать их можно только по времени.
    """
    event = (
        db.query(SecurityEvent)
        .filter(SecurityEvent.camera_id == camera_id,
                SecurityEvent.clip_path.is_(None),
                SecurityEvent.happened_at >= captured_at - window,
                SecurityEvent.happened_at <= captured_at + window)
        .order_by(SecurityEvent.happened_at.desc())
        .first()
    )
    if event is None:
        return None
    event.clip_path = clip_path
    db.commit()
    db.refresh(event)
    return event


def adopt_pending_clip(db: Session, event: SecurityEvent, window: timedelta) -> Optional[MediaItem]:
    """Найти тревожный ролик, приехавший раньше своего события, и связать их.

    Порядок прибытия не гарантирован: рекордер обходит папку по алфавиту, а имена
    видео и снимков начинаются по-разному (`2026-...` против `26-...`), так что
    чанк обычно уезжает первым. Значит связывать надо с обеих сторон, иначе
    половина тревог остаётся без записи.
    """
    clip = (
        db.query(MediaItem)
        .filter(MediaItem.camera_id == event.camera_id,
                MediaItem.kind == "video",
                MediaItem.is_alert.is_(True),
                MediaItem.event_id.is_(None),
                MediaItem.captured_at >= event.happened_at - window,
                MediaItem.captured_at <= event.happened_at + window)
        .order_by(MediaItem.captured_at.desc())
        .first()
    )
    if clip is None:
        return None
    clip.event_id = event.id
    event.clip_path = str(media_module.resolve(clip.rel_path))
    db.commit()
    return clip


# --- media archive --------------------------------------------------------

def find_media(db: Session, camera_id: int, filename: str) -> Optional[MediaItem]:
    return (
        db.query(MediaItem)
        .filter(MediaItem.camera_id == camera_id, MediaItem.filename == filename)
        .one_or_none()
    )


def record_media(db: Session, *, family_id: int, camera: Camera, filename: str, kind: str,
                 rel_path: str, thumb_rel_path: str = None, captured_at: datetime,
                 size_bytes: int = None, is_alert: bool = False, is_merged: bool = False,
                 detected_class: str = None, confidence: float = None, area: int = None,
                 event_id: int = None) -> MediaItem:
    item = MediaItem(
        family_id=family_id, camera_id=camera.id, event_id=event_id,
        filename=filename, kind=kind, rel_path=rel_path, thumb_rel_path=thumb_rel_path,
        captured_at=captured_at, size_bytes=size_bytes, is_alert=is_alert, is_merged=is_merged,
        detected_class=detected_class, confidence=confidence, area=area,
    )
    # Файл приехал — значит камера жива, даже если в кадре ничего интересного.
    if camera.last_seen_at is None or camera.last_seen_at < captured_at:
        camera.last_seen_at = captured_at
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def list_media(db: Session, family_id: int, camera_id: int = None, alerts_only: bool = False,
               page: int = 1) -> Tuple[List[MediaItem], bool]:
    """Страница архива. Возвращает (записи, есть ли ещё)."""
    page = max(1, page)
    query = db.query(MediaItem).filter(MediaItem.family_id == family_id)
    if camera_id:
        query = query.filter(MediaItem.camera_id == camera_id)
    if alerts_only:
        query = query.filter(MediaItem.is_alert.is_(True))

    # Берём на одну запись больше, чем показываем: дешевле, чем считать COUNT(*)
    # по таблице, которая растёт быстрее всех остальных в базе.
    rows = (
        query.order_by(MediaItem.captured_at.desc(), MediaItem.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE + 1)
        .all()
    )
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def get_media(db: Session, family_id: int, media_id: int) -> Optional[MediaItem]:
    item = db.get(MediaItem, media_id)
    return item if item is not None and item.family_id == family_id else None


def latest_media(db: Session, family_id: int, camera_id: int) -> Optional[MediaItem]:
    """Последний кадр камеры — превью её плитки."""
    return (
        db.query(MediaItem)
        .filter(MediaItem.family_id == family_id, MediaItem.camera_id == camera_id,
                MediaItem.kind == "photo")
        .order_by(MediaItem.captured_at.desc())
        .first()
    )


def media_stats(db: Session, family_id: int) -> dict:
    """Сколько всего в архиве — одна строка под фильтрами."""
    rows = (
        db.query(MediaItem.kind, func.count(MediaItem.id), func.coalesce(func.sum(MediaItem.size_bytes), 0))
        .filter(MediaItem.family_id == family_id)
        .group_by(MediaItem.kind)
        .all()
    )
    by_kind = {kind: (count, size) for kind, count, size in rows}
    return {
        "photos": by_kind.get("photo", (0, 0))[0],
        "videos": by_kind.get("video", (0, 0))[0],
        "bytes": sum(size for _, size in by_kind.values()),
    }


def describe(event: SecurityEvent, camera: Camera = None) -> str:
    """One warm, non-dramatic sentence about an event."""
    label = camera.label if camera else "камера"
    what = {"person": "кто-то", "car": "машина"}.get((event.detected_class or "").lower(), "движение")
    return f"{what.capitalize()} у камеры «{label}», {to_local(event.happened_at):%H:%M}. {event.reason}."
