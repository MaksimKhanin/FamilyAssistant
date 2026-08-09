"""Настройки: коннекторы, агент и инструменты, семья и модули, профиль, модель и знания."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.agent import policy
from app.core import connectors as connector_service
from app.core import family as family_service
from app.core import push
from app.core.access import access_matrix, set_module_enabled
from app.core.auth import can_act_as, get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import AUTONOMY_LEVELS, ActionLog, ScheduledJob, User
from app.core.templating import render
from app.modules import togglable
from app.web.context import screen_context

router = APIRouter(prefix="/settings", tags=["settings"])

JOB_LABELS = {
    "morning_digest": ("Утренняя сводка", "08:30"),
    "evening_summary": ("Вечерний итог", "21:00"),
    "weekly_review": ("Разбор недели", "19:00"),
}

CORE_MODELS = [
    ("local", "Локальная 8B", "Ничего не уходит из дома, отвечает проще"),
    ("cloud", "Облачная большая", "Заметно умнее, данные уходят провайдеру"),
    ("hybrid", "Гибрид", "Обычное — локально, сложное — в облако"),
]
VLM_MODES = [("core", "То же ядро", "Одна модель на всё"),
             ("separate", "Отдельная VLM", "Отдельная модель для фото блюд")]
YOLO_MODELS = [("yolov8n", "YOLOv8n", "Легко, хватает для «есть человек / нет»"),
               ("yolov8m", "YOLOv8m", "Точнее, нужен приличный процессор"),
               ("yolov9", "YOLOv9", "Самая точная, нужен GPU")]


# --- коннекторы -----------------------------------------------------------

@router.get("/connectors", response_class=HTMLResponse)
def connectors_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Коннекторы",
                             subtitle="Подключения личные у каждого — начинать стоит с «только читает»")
    context.update(
        connectors=connector_service.overview(db, viewed.id),
        permission_labels=connector_service.PERMISSION_LABELS,
        permission_texts=connector_service.PERMISSION_EXPLANATIONS,
    )
    return render(request, "settings/connectors.html", context)


@router.post("/connectors/{service}")
def update_connector(
    service: str,
    action: str = Form(...),
    permission: str = Form(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed):
        try:
            if action == "toggle":
                rows = connector_service.rows_for(db, viewed.id)
                connected = bool(rows.get(service) and rows[service].connected)
                connector_service.set_connected(db, viewed.id, service, not connected)
            elif action == "permission" and permission:
                connector_service.set_permission(db, viewed.id, service, permission)
        except ValueError:
            pass
    return RedirectResponse("/settings/connectors", status_code=303)


# --- агент и инструменты --------------------------------------------------

@router.get("/agent", response_class=HTMLResponse)
def agent_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Агент и инструменты",
                             subtitle="Что ассистент делает сам, а о чём спрашивает")

    since = datetime.utcnow() - timedelta(days=1)
    context.update(
        autonomy=viewed.autonomy or 0,
        autonomy_levels=AUTONOMY_LEVELS,
        tools=policy.policy_overview(db, viewed),
        jobs=_jobs(db, viewed),
        job_labels=JOB_LABELS,
        recent_actions=(
            db.query(ActionLog)
            .filter(ActionLog.user_id == viewed.id, ActionLog.created_at >= since)
            .order_by(ActionLog.created_at.desc())
            .limit(8)
            .all()
        ),
    )
    return render(request, "settings/agent.html", context)


def _jobs(db: Session, user: User):
    existing = {j.kind: j for j in db.query(ScheduledJob).filter(ScheduledJob.user_id == user.id)}
    result = []
    for kind, (label, default_time) in JOB_LABELS.items():
        job = existing.get(kind)
        result.append({"kind": kind, "label": label,
                       "at_time": job.at_time if job else default_time,
                       "enabled": bool(job and job.enabled)})
    return result


@router.post("/agent/autonomy")
def set_autonomy(
    autonomy: int = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed) and 0 <= autonomy <= 3:
        viewed.autonomy = autonomy
        db.commit()
    return RedirectResponse("/settings/agent", status_code=303)


@router.post("/agent/tools/{tool_name}")
def set_tool_mode(
    tool_name: str,
    mode: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed):
        try:
            policy.set_mode(db, viewed, tool_name, mode)
        except ValueError:
            pass
    return RedirectResponse("/settings/agent", status_code=303)


@router.post("/agent/jobs/{kind}")
def toggle_job(
    kind: str,
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed) and kind in JOB_LABELS:
        job = (
            db.query(ScheduledJob)
            .filter(ScheduledJob.user_id == viewed.id, ScheduledJob.kind == kind)
            .one_or_none()
        )
        if job is None:
            job = ScheduledJob(user_id=viewed.id, kind=kind, at_time=JOB_LABELS[kind][1])
            db.add(job)
        job.enabled = enabled == "on"
        db.commit()
    return RedirectResponse("/settings/agent", status_code=303)


# --- семья и модули -------------------------------------------------------

@router.get("/family", response_class=HTMLResponse)
def family_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Семья и модули",
                             subtitle="Никаких ролей и прав — только включено или выключено")
    module_list = togglable()
    context.update(
        module_list=module_list,
        matrix=access_matrix(db, viewed.family_id, [m.name for m in module_list]),
        can_toggle=current.is_head,
    )
    return render(request, "settings/family.html", context)


@router.post("/family/module")
def toggle_module(
    user_id: int = Form(...),
    module: str = Form(...),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Модули включает только глава семьи — это вся ролевая модель MVP."""
    target = db.get(User, user_id)
    if current.is_head and target is not None and target.family_id == current.family_id:
        set_module_enabled(db, user_id, module, enabled == "on")
    return RedirectResponse("/settings/family", status_code=303)


@router.post("/family/name")
def rename_family(
    name: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if current.is_head and current.family is not None:
        family_service.rename(db, current.family, name)
    return RedirectResponse("/settings/family", status_code=303)


# --- профиль --------------------------------------------------------------

@router.get("/profile", response_class=HTMLResponse)
def profile_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    from app.modules.nutrition import service as nutrition_service
    from app.modules.nutrition.models import GOAL_LABELS

    context = screen_context(request, db, current, viewed,
                             title="Профиль", subtitle="Цель, суточная норма и включённые модули")
    module_list = togglable()
    context.update(
        profile=nutrition_service.get_profile(db, viewed.id),
        goal_labels=GOAL_LABELS,
        push_devices=push.device_count(db, viewed.id),
        module_list=module_list,
        matrix=access_matrix(db, viewed.family_id, [m.name for m in module_list]).get(viewed.id, {}),
        can_toggle=current.is_head,
    )
    return render(request, "settings/profile.html", context)


@router.post("/profile")
def update_profile(
    goal: str = Form(None),
    daily_kcal: int = Form(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    from app.modules.nutrition import service as nutrition_service

    if can_act_as(current, viewed):
        nutrition_service.update_profile(db, viewed.id, daily_kcal=daily_kcal, goal=goal)
    return RedirectResponse("/settings/profile", status_code=303)


# --- модель и знания (только глава семьи) ---------------------------------

@router.get("/model", response_class=HTMLResponse)
def model_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Модель и знания",
                             subtitle="Ядро, зрение и база знаний семьи")
    if not current.is_head:
        return render(request, "settings/model_denied.html", context)

    settings_row = family_service.get_settings(db, viewed.family_id)
    context.update(
        settings_row=settings_row,
        core_models=CORE_MODELS,
        vlm_modes=VLM_MODES,
        yolo_models=YOLO_MODELS,
        rag_sources=family_service.RAG_SOURCES,
        rag_values=family_service.rag_sources(settings_row),
    )
    return render(request, "settings/model.html", context)


@router.post("/model")
async def update_model(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not current.is_head:
        return RedirectResponse("/settings/model", status_code=303)

    form = await request.form()
    settings_row = family_service.get_settings(db, viewed.family_id)

    if form.get("core_model") in {key for key, _, _ in CORE_MODELS}:
        settings_row.core_model = form["core_model"]
    if form.get("vlm_mode") in {key for key, _, _ in VLM_MODES}:
        settings_row.vlm_mode = form["vlm_mode"]
    if form.get("yolo_model") in {key for key, _, _ in YOLO_MODELS}:
        settings_row.yolo_model = form["yolo_model"]
    if "cloud_budget" in form:
        try:
            settings_row.cloud_budget_eur = max(0, min(60, int(form["cloud_budget"])))
        except ValueError:
            pass
    settings_row.frames_stay_home = form.get("frames_stay_home") == "on"
    db.commit()

    family_service.set_rag_sources(
        db, settings_row,
        {key: form.get(f"rag_{key}") == "on" for key in family_service.RAG_SOURCES},
    )
    return RedirectResponse("/settings/model", status_code=303)
