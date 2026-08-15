"""Настройки — и участниковые, и админские.

Разделены они не проверками внутри роутов, а адресами: что кому открыто, знает
`app/core/roles.py`, а следит за этим один заслон (`app/web/gate.py`). Отсюда и
два экрана про одно и то же:

  * `/settings/family` — «Семья» глазами участника: кто в доме и кому что
    включено, без единой кнопки;
  * `/settings/accounts` — «Учётные записи» глазами администратора: завести,
    переименовать, выдать ссылку, сменить роль, удалить, включить модули.

Экран профиля один на обе роли, но у администратора на нём только пароль и
оформление: характера, памяток и самостоятельности у служебной учётки нет.
"""
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.agent import policy
from app.core import accounts
from app.core import connectors as connector_service
from app.core import family as family_service
from app.core import instructions
from app.core import push
from app.core.access import access_matrix, set_module_enabled
from app.core.auth import can_act_as, get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import AUTONOMY_LEVELS, THEMES, ActionLog, ScheduledJob, User
from app.core.templating import render
from app.modules import names as module_names, togglable
from app.web.context import avatar, screen_context
from app.web.routes_invite import invite_url

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
    """Каталог коннекторов — витрина без товара.

    Уровни доступа и сам экран готовы, настоящих интеграций пока нет: тумблер
    ничего не подключал бы, а обещал. Поэтому экран показывается погашенным и с
    плашкой — так честнее, чем прятать его совсем: контракт «что именно человек
    разрешил сервису» уже зафиксирован и никуда не денется.
    """
    context = screen_context(request, db, current, viewed,
                             title="Коннекторы",
                             subtitle="Раздел в разработке — подключений пока нет")
    context.update(
        connectors=connector_service.overview(db, viewed.id),
        permission_labels=connector_service.PERMISSION_LABELS,
        permission_texts=connector_service.PERMISSION_EXPLANATIONS,
    )
    return render(request, "settings/connectors.html", context)


@router.post("/connectors/{service}")
def update_connector(service: str):
    """Пока экран в разработке, менять нечего: тумблеры на нём погашены."""
    return RedirectResponse("/settings/connectors", status_code=303)


# --- агент и инструменты (администратор, на всю семью) --------------------

@router.get("/agent", response_class=HTMLResponse)
def agent_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Самостоятельность ассистента и режимы инструментов — одни на всю семью.

    Раньше этот экран был личным и жил внутри «Профиля»; теперь он админский и
    задаёт правила сразу для всех (ADR-0008). «Насколько ассистенту можно
    действовать без спроса» — это про доверие в доме, а не про настроение
    отдельного человека, и разными у домашних эти дырки быть не должны.
    """
    context = screen_context(request, db, current, viewed,
                             title="Агент и инструменты",
                             subtitle="Насколько ассистент самостоятелен — одинаково для всех")
    context.update(
        autonomy=family_service.get_settings(db, current.family_id).autonomy or 0,
        autonomy_levels=AUTONOMY_LEVELS,
        tools=policy.policy_overview(db, current.family_id),
    )
    return render(request, "settings/agent.html", context)


@router.post("/agent/autonomy")
def set_autonomy(
    autonomy: int = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    policy.set_autonomy(db, current.family_id, autonomy)
    return RedirectResponse("/settings/agent", status_code=303)


@router.post("/agent/tools/{tool_name}")
def set_tool_mode(
    tool_name: str,
    mode: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        policy.set_mode(db, current.family_id, tool_name, mode)
    except ValueError:
        pass
    return RedirectResponse("/settings/agent", status_code=303)


# --- расписания участника -------------------------------------------------

def _jobs(db: Session, user: User):
    existing = {j.kind: j for j in db.query(ScheduledJob).filter(ScheduledJob.user_id == user.id)}
    result = []
    for kind, (label, default_time) in JOB_LABELS.items():
        job = existing.get(kind)
        result.append({"kind": kind, "label": label,
                       "at_time": job.at_time if job else default_time,
                       "enabled": bool(job and job.enabled)})
    return result


@router.post("/profile/jobs/{kind}")
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
    return RedirectResponse("/settings/profile", status_code=303)


# --- семья глазами участника ----------------------------------------------

@router.get("/family", response_class=HTMLResponse)
def family_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Кто в семье и кому что включено — и ни одной кнопки.

    Учётными записями распоряжается администратор (`/settings/accounts`), но
    знать, кто есть в доме, участнику нужно: с этими людьми он делится досками.
    """
    module_list = togglable()
    members = family_service.members(db, viewed.family_id)
    matrix = access_matrix(db, viewed.family_id, [m.name for m in module_list])

    context = screen_context(request, db, current, viewed,
                             title="Семья",
                             subtitle="Кто пользуется ассистентом в доме")
    context.update(
        module_list=module_list,
        members_info=[{
            "user": member,
            "avatar": avatar(member),
            "modules": [m.title for m in module_list if matrix.get(member.id, {}).get(m.name)],
        } for member in members],
    )
    return render(request, "settings/family.html", context)


# --- учётные записи (администратор) ---------------------------------------

@router.get("/accounts", response_class=HTMLResponse)
def accounts_screen(
    request: Request,
    notice: str = None,
    error: str = None,
    invited: int = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Учётные записи",
                             subtitle="Кто заходит в панель, кому что включено и кто здесь администратор")
    module_list = togglable()
    invited_user = db.get(User, invited) if invited else None

    members_info = accounts.overview(db, current)
    for row in members_info:
        row["avatar"] = avatar(row["user"])
        # Ссылка живёт в карточке человека, пока он ею не воспользовался. Раньше
        # она показывалась ровно один раз — после выпуска — и, стоило уйти с
        # экрана, найти её было негде: оставалось выпускать новую.
        row["invite_link"] = invite_url(row["user"], request)

    context.update(
        module_list=module_list,
        matrix=access_matrix(db, viewed.family_id, [m.name for m in module_list]),
        members_info=members_info,
        notice=notice,
        error=error,
        # Кого только что пригласили — у того карточка подсвечена: список длинный,
        # и после «Добавить участника» надо видеть, куда смотреть.
        invited_id=(invited_user.id
                    if invited_user is not None and invited_user.family_id == viewed.family_id
                    else None),
    )
    return render(request, "settings/accounts.html", context)


def _back(notice: str = None, error: str = None, invited: int = None) -> RedirectResponse:
    params = {k: v for k, v in (("notice", notice), ("error", error), ("invited", invited)) if v}
    query = "?" + urlencode(params) if params else ""
    return RedirectResponse(f"/settings/accounts{query}", status_code=303)


@router.post("/accounts/module")
def toggle_module(
    user_id: int = Form(...),
    module: str = Form(...),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Модули включает администратор — и включает их участнику, а не себе."""
    target = db.get(User, user_id)
    if target is not None and target.family_id == current.family_id and target.is_member:
        set_module_enabled(db, user_id, module, enabled == "on")
    return _back()


@router.post("/accounts/member")
def add_member(
    display_name: str = Form(...),
    relation: str = Form(""),
    username: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        member = accounts.create_member(db, current, display_name, relation, username)
    except accounts.AccountError as e:
        return _back(error=str(e))
    return _back(notice=f"Добавил: {member.display_name}, логин «{member.username}». "
                        f"Ссылка на пароль — в его карточке.",
                 invited=member.id)


@router.post("/accounts/member/{user_id}/rename")
def rename_member(
    user_id: int,
    display_name: str = Form(...),
    relation: str = Form(""),
    username: str = Form(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        member = accounts.rename(db, current, user_id, display_name, relation, username)
    except accounts.AccountError as e:
        return _back(error=str(e))
    return _back(notice=f"Сохранил: {member.display_name}, логин «{member.username}».")


@router.post("/accounts/member/{user_id}/role")
def set_member_role(
    user_id: int,
    admin: str = Form("off"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Сделать учётку административной или вернуть её в участники.

    Роль меняет учётку целиком: у администратора нет ни разговора, ни модулей,
    у участника — админ-раздела. Записи при этом остаются, поэтому шаг обратим.
    """
    try:
        member = accounts.set_admin(db, current, user_id, admin == "on")
    except accounts.AccountError as e:
        return _back(error=str(e))
    role = "администратор" if member.is_admin else "участник"
    return _back(notice=f"{member.display_name} теперь {role}.")


@router.post("/accounts/member/{user_id}/invite")
def issue_invite(
    user_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Новая ссылка-приглашение. Она же сброс пароля: старый перестаёт работать."""
    try:
        member = accounts.issue_invite(db, current, user_id)
    except accounts.AccountError as e:
        return _back(error=str(e))
    return _back(notice=f"Новая ссылка для {member.display_name}. "
                        f"Старый пароль больше не работает.",
                 invited=member.id)


@router.post("/accounts/member/{user_id}/revoke")
def revoke_invite(
    user_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        member = accounts.revoke_invite(db, current, user_id)
    except accounts.AccountError as e:
        return _back(error=str(e))
    return _back(notice=f"Ссылка для {member.display_name} отозвана.")


@router.post("/accounts/member/{user_id}/delete")
def delete_member(
    user_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    try:
        name = accounts.delete_member(db, current, user_id)
    except accounts.AccountError as e:
        return _back(error=str(e))
    return _back(notice=f"Удалил: {name}. Записи и переписка тоже.")


@router.post("/accounts/name")
def rename_family(
    name: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if current.family is not None:
        family_service.rename(db, current.family, name)
    return RedirectResponse("/settings/accounts", status_code=303)


# --- профиль --------------------------------------------------------------

@router.get("/profile", response_class=HTMLResponse)
def profile_screen(
    request: Request,
    notice: str = None,
    error: str = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    from app.modules.memory import knowledge
    from app.modules.nutrition import service as nutrition_service
    from app.modules.nutrition.models import GOAL_LABELS

    # Экран один на обе роли, но у администратора на нём только то, что есть у
    # любой учётки: пароль и оформление. Личного у служебной учётки нет.
    if current.is_admin:
        context = screen_context(request, db, current, viewed,
                                 title="Профиль",
                                 subtitle="Пароль и оформление панели")
        context.update(themes=THEMES, current_theme=current.theme,
                       notice=notice, error=error)
        return render(request, "settings/profile.html", context)

    context = screen_context(request, db, current, viewed,
                             title="Профиль и агент",
                             subtitle="Оформление, характер ассистента и памятки по областям")
    module_list = togglable()
    since = datetime.utcnow() - timedelta(days=1)
    # Памятка есть только у включённых областей: выключенный модуль не отдаёт ни
    # экранов, ни инструментов — и поля, которое никуда не поедет, тоже.
    memo_modules = instructions.memo_modules(
        db, viewed.id, [name for name in module_names() if name in context["enabled_modules"]],
    )
    context.update(
        profile=nutrition_service.get_profile(db, viewed.id),
        goal_labels=GOAL_LABELS,
        # На экране — только своё: умолчание стоит подсказкой в пустом поле, иначе
        # человек видит текст, который он не писал, и не решается его стереть.
        character=instructions.own_character(viewed),
        default_character=instructions.DEFAULT_CHARACTER,
        character_limit=instructions.CHARACTER_LIMIT,
        # Правила показываются рядом с характером, но правятся на своей доске:
        # заводит их разговор, а редактор у них уже есть — экран знаний.
        rules=knowledge.list_rules(db, viewed.id),
        rules_url=knowledge.rules_url(db, viewed.id),
        memo_modules=memo_modules,
        memo_limit=instructions.MEMO_LIMIT,
        push_devices=push.device_count(db, viewed.id),
        notice=notice,
        error=error,
        module_list=module_list,
        # Модули человек видит, но не переключает: их включает администратор.
        matrix=access_matrix(db, viewed.family_id, [m.name for m in module_list]).get(viewed.id, {}),
        themes=THEMES,
        # Оформление меняет себе тот, кто смотрит: режим «от лица» переключает
        # данные экрана, а не глаза человека перед телефоном.
        current_theme=current.theme,
        # Самостоятельность на экране только показана: задаёт её администратор
        # сразу для всей семьи (ADR-0008).
        autonomy=family_service.get_settings(db, viewed.family_id).autonomy or 0,
        autonomy_levels=AUTONOMY_LEVELS,
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
    return render(request, "settings/profile.html", context)


@router.post("/profile/theme")
def set_theme(
    theme: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Оформление своё у каждого — и меняет его человек всегда себе."""
    if theme in THEMES:
        current.theme = theme
        db.commit()
    return RedirectResponse("/settings/profile", status_code=303)


@router.post("/profile/password")
def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_repeat: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Свой пароль человек меняет сам — чужой сбрасывает администратор ссылкой."""
    try:
        accounts.change_own_password(db, current, current_password,
                                     new_password, new_password_repeat)
    except accounts.AccountError as e:
        return RedirectResponse(f"/settings/profile?{urlencode({'error': str(e)})}", status_code=303)
    return RedirectResponse(f"/settings/profile?{urlencode({'notice': 'Пароль изменён.'})}",
                            status_code=303)


@router.post("/profile/character")
def update_character(
    character: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Характер ассистента — свой у каждого, как и оформление."""
    if can_act_as(current, viewed):
        instructions.set_character(db, viewed, character)
    return RedirectResponse("/settings/profile", status_code=303)


@router.post("/profile/memo/{module}")
def update_memo(
    module: str,
    memo: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Памятка одной области. Сохраняется по одной: полей на экране несколько,
    но человек правит их по очереди, и общая кнопка «Сохранить» внизу заставляла
    бы его помнить, что он трогал."""
    if can_act_as(current, viewed) and module in module_names():
        instructions.set_memo(db, viewed.id, module, memo)
    return RedirectResponse("/settings/profile", status_code=303)


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


# --- модель и знания (администратор) --------------------------------------

@router.get("/model", response_class=HTMLResponse)
def model_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Модель и знания",
                             subtitle="Ядро, зрение, деньги и база знаний семьи")
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
