"""Web screens for the nutrition module: приём пищи, статистика, активность, план."""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.agent.llm import LLMUnavailable
from app.agent.runtime import run_tool_directly
from app.core.auth import can_act_as, can_see_figures, get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.events import ACTIVITY_LOGGED, MEAL_CONFIRMED, MEAL_LOGGED, bus
from app.core.models import User
from app.core.templating import render
from app.modules.nutrition import service
from app.modules.nutrition.models import (
    ACTIVITY_KCAL, ACTIVITY_LABELS, ACTIVITY_UNITS, GOAL_LABELS, SOURCE_PHOTO, SOURCE_TEXT,
)
from app.modules.nutrition.service import PERIOD_LABELS
from app.modules.nutrition.vision import estimate_from_image, safe_estimate_from_text
from app.web.context import screen_context

router = APIRouter(prefix="/nutrition", tags=["nutrition"])

QUICK_PHRASES = ["Кофе с молоком", "Овсянка с ягодами", "Суп и салат", "Куриная грудка с рисом"]


def _private_notice(request: Request, db: Session, current: User, viewed: User, title: str, subtitle: str):
    """Screens of another family member show no figures — that is the whole privacy model."""
    context = screen_context(request, db, current, viewed, title=title, subtitle=subtitle)
    context["owner_name"] = viewed.display_name
    return render(request, "nutrition/private.html", context, status_code=200)


@router.get("/meal", response_class=HTMLResponse)
def meal_screen(
    request: Request,
    meal_id: int = None,
    state: str = "input",
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not can_see_figures(current, viewed):
        return _private_notice(request, db, current, viewed, "Приём пищи",
                               "Записи о еде видит только их владелец")

    context = screen_context(request, db, current, viewed,
                             title="Приём пищи", subtitle="Фото или пара слов — я оценю и запишу")
    meal = service.get_meal(db, viewed.id, meal_id) if meal_id else None
    context.update(
        meal=meal,
        state=state if meal is not None else "input",
        today_meals=service.meals_for_day(db, viewed.id),
        quick_phrases=QUICK_PHRASES,
        error=request.query_params.get("error"),
    )
    return render(request, "nutrition/meal.html", context)


@router.post("/meal/estimate")
async def estimate_meal(
    text: str = Form(""),
    photo: UploadFile = File(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not can_act_as(current, viewed) or not can_see_figures(current, viewed):
        return RedirectResponse("/nutrition/meal", status_code=303)

    image_bytes = await photo.read() if photo is not None and photo.filename else None
    if not image_bytes and not text.strip():
        return RedirectResponse("/nutrition/meal?error=empty", status_code=303)

    if image_bytes:
        try:
            estimate = estimate_from_image(image_bytes, hint=text or None)
        except LLMUnavailable:
            return RedirectResponse("/nutrition/meal?error=vision", status_code=303)
        image_path = service.save_image(image_bytes, viewed.id)
        meal = service.create_draft(db, viewed.id, estimate, source=SOURCE_PHOTO,
                                    raw_input=text or None, image_path=image_path)
    else:
        estimate = safe_estimate_from_text(text)
        meal = service.create_draft(db, viewed.id, estimate, source=SOURCE_TEXT, raw_input=text)

    bus.publish(MEAL_LOGGED, {"meal_id": meal.id, "user_id": viewed.id})
    return RedirectResponse(f"/nutrition/meal?meal_id={meal.id}&state=estimate", status_code=303)


@router.post("/meal/{meal_id}/confirm")
def confirm_meal(
    meal_id: int,
    kcal: int = Form(...),
    protein: int = Form(...),
    fat: int = Form(...),
    carbs: int = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not can_act_as(current, viewed):
        return RedirectResponse("/nutrition/meal", status_code=303)

    meal = service.confirm_meal(db, viewed.id, meal_id,
                                {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs})
    if meal is None:
        return RedirectResponse("/nutrition/meal", status_code=303)

    bus.publish(MEAL_CONFIRMED, {"meal_id": meal.id, "user_id": viewed.id})
    return RedirectResponse(f"/nutrition/meal?meal_id={meal.id}&state=done", status_code=303)


@router.post("/meal/{meal_id}/discard")
def discard_meal(
    meal_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """«Это не то блюдо» — черновик удаляется, экран возвращается к вводу."""
    if can_act_as(current, viewed):
        service.delete_meal(db, viewed.id, meal_id)
    return RedirectResponse("/nutrition/meal", status_code=303)


@router.get("/stats", response_class=HTMLResponse)
def stats_screen(
    request: Request,
    period: str = "week",
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not can_see_figures(current, viewed):
        return _private_notice(request, db, current, viewed, "Статистика питания",
                               "Цифры питания видит только их владелец")

    period = period if period in PERIOD_LABELS else "week"
    stats = service.period_stats(db, viewed.id, period)
    peak = max([max(d.consumed, d.burned) for d in stats.days] + [stats.norm, 1])

    context = screen_context(request, db, current, viewed,
                             title="Статистика питания", subtitle="Спокойный взгляд на баланс дня")
    context.update(stats=stats, period=period, period_labels=PERIOD_LABELS, chart_peak=peak,
                   profile=service.get_profile(db, viewed.id))
    return render(request, "nutrition/stats.html", context)


@router.get("/activity", response_class=HTMLResponse)
def activity_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not can_see_figures(current, viewed):
        return _private_notice(request, db, current, viewed, "Активность",
                               "Записи об активности видит только их владелец")

    context = screen_context(request, db, current, viewed,
                             title="Активность", subtitle="Шаги и тренировки — вручную, без датчиков")
    context.update(
        today=service.activity_for_day(db, viewed.id),
        kinds=[(k, ACTIVITY_LABELS[k], ACTIVITY_UNITS[k], ACTIVITY_KCAL[k]) for k in ACTIVITY_KCAL],
    )
    return render(request, "nutrition/activity.html", context)


@router.post("/activity")
def add_activity(
    kind: str = Form(...),
    value: float = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed) and kind in ACTIVITY_KCAL and value > 0:
        entry = service.log_activity(db, viewed.id, kind, value)
        bus.publish(ACTIVITY_LOGGED, {"activity_id": entry.id, "user_id": viewed.id})
    return RedirectResponse("/nutrition/activity", status_code=303)


@router.get("/plan", response_class=HTMLResponse)
def plan_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not can_see_figures(current, viewed):
        return _private_notice(request, db, current, viewed, "План питания",
                               "Идеи питания видит только их владелец")

    context = screen_context(request, db, current, viewed,
                             title="План питания", subtitle="Это идеи, а не предписание")
    # План не хранится: он собирается по запросу («Предложить другое» → POST).
    context.update(plan=None, error=None, goal_labels=GOAL_LABELS,
                   profile=service.get_profile(db, viewed.id))
    return render(request, "nutrition/plan.html", context)


@router.post("/plan")
def build_plan(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """«Предложить другое» — вызывает тот же инструмент, что и агент в чате."""
    if not can_act_as(current, viewed):
        return RedirectResponse("/nutrition/plan", status_code=303)

    result = run_tool_directly(db, viewed, "suggest_meal_plan", {}, mode="web", actor=current)
    context = screen_context(request, db, current, viewed,
                             title="План питания", subtitle="Это идеи, а не предписание")
    context.update(plan=(result.card or {}) if result.ok else None,
                   error=None if result.ok else result.summary,
                   goal_labels=GOAL_LABELS, profile=service.get_profile(db, viewed.id))
    return render(request, "nutrition/plan.html", context)
