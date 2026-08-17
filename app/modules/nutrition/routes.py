"""Web screens for the nutrition module: приём пищи, статистика, активность, план."""
from datetime import date
from urllib.parse import urlencode

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
    ACTIVITY_CEILING, ACTIVITY_KCAL, ACTIVITY_LABELS, ACTIVITY_UNITS, GOAL_LABELS, SOURCE_PHOTO, SOURCE_TEXT,
)
from app.modules.nutrition.service import PERIOD_LABELS, PERIOD_WINDOWS
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


def _safe_back(back: str, fallback: str) -> str:
    """Куда вернуть человека после действия — только внутрь панели.

    Адрес приезжает из формы, а значит, из браузера: пускать по нему куда угодно
    незачем. Всё, что не начинается с одного «/», — не наш экран.
    """
    if back and back.startswith("/") and not back.startswith("//"):
        return back
    return fallback


@router.post("/meal/{meal_id}/discard")
def discard_meal(
    request: Request,
    meal_id: int,
    back: str = Form(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Удалить запись о еде — и ошибочный черновик, и уже подтверждённую.

    Один обработчик на две точки входа, потому что дело одно и то же. С экрана
    приходит обычная форма и получает переход, из чата — запрос htmx, и тогда в
    ленту возвращается реплика ассистента: человек видит подтверждение там же,
    где нажал, а не на внезапно сменившемся экране.

    Различает их не «пришло ли через htmx» — через него приходит и переход
    (`hx-boost`, ADR-0001), — а `HX-Boosted`: боту чата отвечаем репликой, экрану
    переходом обратно на него же.
    """
    meal = service.get_meal(db, viewed.id, meal_id) if can_act_as(current, viewed) else None
    if meal is not None:
        title, kcal = meal.title, meal.kcal
        service.delete_meal(db, viewed.id, meal_id)
        said = f"Удалил запись: {title} — {kcal} ккал. В дневном балансе она больше не считается."
    else:
        said = "Эту запись уже не удалить — возможно, её удалили раньше."

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return render(request, "partials/chat_messages.html",
                      {"request": request,
                       "messages": [{"role": "assistant", "text": said, "traces": [], "cards": []}]})
    return RedirectResponse(_safe_back(back, "/nutrition/meal"), status_code=303)


@router.post("/activity/{activity_id}/discard")
def discard_activity(
    activity_id: int,
    back: str = Form(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Убрать одну запись об активности. `back` — экран, с которого нажали."""
    if can_act_as(current, viewed):
        service.delete_activity(db, viewed.id, activity_id)
    return RedirectResponse(_safe_back(back, "/nutrition/activity"), status_code=303)


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
    # Записи периода — под графиком: столбик без строк, из которых он сложился,
    # нечем поправить, и «убрать лишнее» превращается в разговор с ассистентом.
    context.update(stats=stats, period=period, period_labels=PERIOD_LABELS, chart_peak=peak,
                   period_windows=PERIOD_WINDOWS, profile=service.get_profile(db, viewed.id),
                   records=service.records_for_period(db, viewed.id, period),
                   notice=request.query_params.get("notice"))
    return render(request, "nutrition/stats.html", context)


def _back_to_stats(period: str, notice: str = None) -> RedirectResponse:
    params = {"period": period if period in PERIOD_LABELS else "week"}
    if notice:
        params["notice"] = notice
    return RedirectResponse(f"/nutrition/stats?{urlencode(params)}", status_code=303)


@router.post("/stats/day/{day}/clear")
def clear_day(
    day: str,
    period: str = Form("week"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Убрать все записи одного дня — «этот день записан неверно, сотрите»."""
    if not can_act_as(current, viewed) or not can_see_figures(current, viewed):
        return _back_to_stats(period)

    try:
        chosen = date.fromisoformat(day)
    except ValueError:
        return _back_to_stats(period)

    removed = service.clear_day(db, viewed.id, chosen)
    notice = (f"Убрал за {chosen.strftime('%d.%m')}: {removed.words}."
              if removed else f"За {chosen.strftime('%d.%m')} убирать было нечего.")
    return _back_to_stats(period, notice)


@router.post("/stats/clear")
def clear_period(
    period: str = Form("week"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Убрать всё за период, который человек сейчас видит на экране."""
    if not can_act_as(current, viewed) or not can_see_figures(current, viewed):
        return _back_to_stats(period)

    period = period if period in PERIOD_LABELS else "week"
    removed = service.clear_period(db, viewed.id, period)
    window = PERIOD_WINDOWS[period]
    notice = (f"Убрал {window}: {removed.words}. Цифры пересчитаны."
              if removed else f"{window.capitalize()} убирать было нечего.")
    return _back_to_stats(period, notice)


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
    ceiling = ACTIVITY_CEILING.get(kind)
    if (can_act_as(current, viewed) and kind in ACTIVITY_KCAL and value > 0
            and not (ceiling and value > ceiling)):
        entry = service.log_activity(db, viewed.id, kind, value)
        bus.publish(ACTIVITY_LOGGED, {"activity_id": entry.id, "user_id": viewed.id})
    return RedirectResponse("/nutrition/activity", status_code=303)


def _plan_context(request: Request, db: Session, current: User, viewed: User,
                  error: str = None, comment: str = None) -> dict:
    """Экран плана целиком: подбор, закреп и пожелания к рациону.

    Подбор теперь хранится (`meal_ideas`), поэтому собирается контекст одинаково —
    и при переходе на экран, и сразу после кнопки «Предложить идеи».
    """
    context = screen_context(request, db, current, viewed,
                             title="План питания", subtitle="Это идеи, а не предписание")
    context.update(days=service.plan_days(db, viewed.id),
                   saved=service.saved_ideas(db, viewed.id),
                   profile=service.get_profile(db, viewed.id),
                   preferences_limit=service.PREFERENCES_LIMIT,
                   goal_labels=GOAL_LABELS, error=error, comment=comment)
    return context


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

    return render(request, "nutrition/plan.html", _plan_context(request, db, current, viewed))


@router.post("/plan")
def build_plan(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """«Предложить идеи» — собрать рацион на несколько дней и сохранить его.

    Инструмент тот же, что в логе действий, но из чата он не вызывается: в
    разговоре на «что поесть» отвечают одним блюдом (ADR-0010).
    """
    if not can_act_as(current, viewed) or not can_see_figures(current, viewed):
        return RedirectResponse("/nutrition/plan", status_code=303)

    result = run_tool_directly(db, viewed, "suggest_meal_plan", {}, mode="web", actor=current)
    comment = (result.card or {}).get("comment") if result.ok else None
    return render(request, "nutrition/plan.html",
                  _plan_context(request, db, current, viewed,
                                error=None if result.ok else result.summary, comment=comment))


@router.post("/plan/preferences")
def save_preferences(
    preferences: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Пожелания к рациону — то, что человек ест и чего не ест, его словами."""
    if can_act_as(current, viewed) and can_see_figures(current, viewed):
        service.set_preferences(db, viewed.id, preferences)
    return RedirectResponse("/nutrition/plan", status_code=303)


@router.post("/plan/dish/{idea_id}/save")
def toggle_dish(
    idea_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Отметить блюдо или снять отметку. Отмеченное переживает следующий подбор."""
    if can_act_as(current, viewed) and can_see_figures(current, viewed):
        service.toggle_saved(db, viewed.id, idea_id)
    return RedirectResponse("/nutrition/plan", status_code=303)


@router.post("/plan/dish/{idea_id}/recipe")
def dish_recipe(
    request: Request,
    idea_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """«Рецепт» у блюда — расписать и сохранить прямо у него.

    Расписывается по просьбе, а не для всего подбора сразу: рецепт стоит
    обращения к модели, а открывают его у одного-двух блюд из дюжины.
    """
    if not can_act_as(current, viewed) or not can_see_figures(current, viewed):
        return RedirectResponse("/nutrition/plan", status_code=303)

    idea = service.get_idea(db, viewed.id, idea_id)
    if idea is None:
        return RedirectResponse("/nutrition/plan", status_code=303)

    result = run_tool_directly(db, viewed, "dish_recipe", {"name": idea.title},
                               mode="web", actor=current)
    if result.ok:
        service.set_recipe(db, viewed.id, idea_id, result.data.get("recipe", ""))
        return RedirectResponse(f"/nutrition/plan#dish-{idea_id}", status_code=303)
    return render(request, "nutrition/plan.html",
                  _plan_context(request, db, current, viewed, error=result.summary))


@router.post("/plan/dishes")
def keep_dish(
    request: Request,
    title: str = Form(...),
    slot: str = Form(""),
    kcal: int = Form(0),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """«Оставить» на карточке блюда из разговора — блюдо уезжает в план питания.

    Отвечает так же, как удаление записи из чата: репликой в ленту, если нажали
    в разговоре, и переходом на экран, если нажали на экране.
    """
    idea = (service.keep_dish(db, viewed.id, title, slot=slot, kcal=kcal)
            if can_act_as(current, viewed) and can_see_figures(current, viewed) else None)
    said = (f"Отметил блюдо: {idea.title}. Оно теперь в плане питания — там же можно "
            f"попросить рецепт." if idea is not None
            else "Не получилось отметить это блюдо.")

    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return render(request, "partials/chat_messages.html",
                      {"request": request,
                       "messages": [{"role": "assistant", "text": said, "traces": [], "cards": []}]})
    return RedirectResponse("/nutrition/plan", status_code=303)
