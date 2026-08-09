"""Security domain logic — and, above all, the filter.

The point of this module is not analytics. It is that nobody in the family should
ever have to scroll through camera footage: they see a notification when something
is genuinely worth a look, and everything else quietly lands in a log.

The filter is a cascade, cheapest first:

    YOLO на edge  →  человек/машина в кадре вообще есть?   (иначе кадр не доедет сюда)
    правила       →  время, зона, класс объекта             (детерминированно, мгновенно)
    модель        →  только для спорных случаев             (classify_event, по желанию)

`decide` is the rules step and is pure — no database, no clock, no I/O — so the
household's «что считать тревогой» is one readable function that can be tested.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.clock import days_ago_start_utc, to_local, utc_now
from app.core.logging import get_logger
from app.modules.security.models import (
    VERDICT_ANOMALY, VERDICT_CHECK, VERDICT_NORMAL, Camera, SecurityEvent,
)

logger = get_logger("security")

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


def list_events(db: Session, family_id: int, only: str = "all", days: int = 7,
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


def mark_ours(db: Session, family_id: int, event_id: int) -> Optional[SecurityEvent]:
    """«Это свои, всё хорошо» — снимает тревогу, не удаляя запись."""
    event = get_event(db, family_id, event_id)
    if event is None:
        return None
    event.resolution = "ours"
    event.resolved_at = utc_now()
    db.commit()
    return event


def describe(event: SecurityEvent, camera: Camera = None) -> str:
    """One warm, non-dramatic sentence about an event."""
    label = camera.label if camera else "камера"
    what = {"person": "кто-то", "car": "машина"}.get((event.detected_class or "").lower(), "движение")
    return f"{what.capitalize()} у камеры «{label}», {to_local(event.happened_at):%H:%M}. {event.reason}."
