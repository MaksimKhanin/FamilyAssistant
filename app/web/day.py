"""Цифры дня — питание и дом, — которые показывает «Главная».

Приватность здесь одна на всех: чужие цифры питания не показываются никому,
кому их не открыли (`can_see_figures`), а выключенный модуль не отдаёт ничего —
не ноль, а именно ничего, чтобы плитка не рисовалась вовсе.
"""
from typing import Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.core.auth import can_see_figures
from app.core.models import User


def nutrition_day(db: Session, current: User, viewed: User, enabled: Set[str]) -> Tuple[object, object]:
    """Итоги сегодняшнего дня и профиль — или `(None, None)`, если смотреть нечего."""
    if "nutrition" not in enabled or not can_see_figures(current, viewed):
        return None, None

    from app.modules.nutrition import service as nutrition_service

    stats = nutrition_service.period_stats(db, viewed.id, "day")
    return stats.today, nutrition_service.get_profile(db, viewed.id)


def home_summary(db: Session, viewed: User, enabled: Set[str]) -> Optional[dict]:
    """Дом за сутки: камеры, события и то, из-за чего стоит взглянуть."""
    if "security" not in enabled:
        return None

    from app.modules.security import service as security_service

    cameras = security_service.list_cameras(db, viewed.family_id)
    return {
        "cameras_total": len(cameras),
        "cameras_notifying": sum(1 for c in cameras if c.notify_enabled),
        "anomalies": security_service.anomaly_count(db, viewed.family_id, days=1),
        "events": len(security_service.list_events(db, viewed.family_id, days=1)),
    }
