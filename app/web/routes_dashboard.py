"""Главная — спокойный обзор дня: питание, дом, быстрые действия, полоса семьи."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core import family as family_service
from app.core.access import is_module_enabled
from app.core.auth import can_see_figures, get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.web.context import avatar, screen_context

router = APIRouter(tags=["dashboard"])


def _greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Доброе утро"
    if 12 <= hour < 18:
        return "Добрый день"
    if 18 <= hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    from datetime import datetime

    context = screen_context(request, db, current, viewed,
                             title="Главная", subtitle="Спокойный обзор дня — питание и дом")

    nutrition_on = is_module_enabled(db, viewed.id, "nutrition")
    security_on = is_module_enabled(db, viewed.id, "security")

    day = None
    profile = None
    steps_today = 0
    if nutrition_on and can_see_figures(current, viewed):
        from app.modules.nutrition import service as nutrition_service
        stats = nutrition_service.period_stats(db, viewed.id, "day")
        day = stats.today
        profile = nutrition_service.get_profile(db, viewed.id)
        steps_today = sum(int(a.value) for a in nutrition_service.activity_for_day(db, viewed.id)
                          if a.kind == "steps")

    home = None
    if security_on:
        from app.modules.security import service as security_service
        cameras = security_service.list_cameras(db, viewed.family_id)
        home = {
            "cameras_total": len(cameras),
            "cameras_notifying": sum(1 for c in cameras if c.notify_enabled),
            "anomalies": security_service.anomaly_count(db, viewed.family_id, days=1),
            "events": len(security_service.list_events(db, viewed.family_id, days=1)),
        }

    context.update(
        greeting=_greeting(datetime.now().hour),
        nutrition_on=nutrition_on,
        security_on=security_on,
        day=day,
        profile=profile,
        steps_today=steps_today,
        home=home,
        ring_degrees=_ring_degrees(day, profile),
        family_strip=_family_strip(db, current, viewed) if current.is_head else None,
    )
    return render(request, "dashboard.html", context)


def _ring_degrees(day, profile) -> int:
    if not day or not profile or not profile.daily_kcal:
        return 0
    return max(0, min(360, round(day.consumed / profile.daily_kcal * 360)))


def _family_strip(db: Session, current: User, viewed: User):
    """«Сегодня у семьи» — кто что записал, но без чужих цифр.

    The head of the family sees that everyone is fed and when the last entry was;
    the calories themselves belong to their owner and stay hidden.
    """
    from app.modules.nutrition import service as nutrition_service

    rows = []
    for member in family_service.members(db, viewed.family_id):
        nutrition_on = is_module_enabled(db, member.id, "nutrition")
        security_on = is_module_enabled(db, member.id, "security")
        last_meal = None
        progress = 0
        if nutrition_on:
            meals = nutrition_service.meals_for_day(db, member.id)
            last_meal = meals[-1] if meals else None
            if current.id == member.id:
                profile = nutrition_service.get_profile(db, member.id)
                consumed = sum(m.kcal for m in meals)
                progress = min(100, round(consumed / profile.daily_kcal * 100)) if profile.daily_kcal else 0

        rows.append({
            "avatar": avatar(member),
            "user": member,
            "modules_label": _modules_label(nutrition_on, security_on),
            "own": current.id == member.id,
            "progress": progress,
            "last_meal_at": last_meal.eaten_at if last_meal else None,
        })
    return rows


def _modules_label(nutrition_on: bool, security_on: bool) -> str:
    if nutrition_on and security_on:
        return "оба модуля"
    if nutrition_on:
        return "питание"
    if security_on:
        return "дом"
    return "выключено"
