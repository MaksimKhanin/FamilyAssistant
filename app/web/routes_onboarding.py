"""Онбординг в три шага: название семьи → участники → модули.

Deliberately not a wizard you must finish: every step is also reachable later from
Настройки. A family of five should be able to change its mind about who gets which
module without going through setup again.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core import accounts
from app.core import family as family_service
from app.core.access import access_matrix, set_module_enabled
from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.modules import togglable
from app.web.context import avatar, screen_context
from app.web.routes_invite import invite_url

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

STEPS = 3


@router.get("", response_class=HTMLResponse)
def onboarding(
    request: Request,
    step: int = 1,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    step = max(1, min(STEPS, step))
    module_list = togglable()
    members = family_service.members(db, current.family_id)

    context = screen_context(request, db, current, viewed,
                             title="Семья", subtitle="Три коротких шага — и можно пользоваться")
    context.update(
        step=step,
        steps=STEPS,
        module_list=module_list,
        matrix=access_matrix(db, current.family_id, [m.name for m in module_list]),
        member_rows=[{"user": m, "avatar": avatar(m), "invite_url": invite_url(m, request)}
                     for m in members],
        can_toggle=True,
    )
    return render(request, "onboarding.html", context)


@router.post("/name")
def set_name(
    name: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if current.family is not None:
        family_service.rename(db, current.family, name)
    return RedirectResponse("/onboarding?step=2", status_code=303)


@router.post("/member")
def add_member(
    display_name: str = Form(...),
    relation: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Завести человека. Пароль он придумает сам по ссылке-приглашению."""
    try:
        accounts.create_member(db, current, display_name, relation)
    except accounts.AccountError:
        pass          # подробный разбор ошибок — на экране «Учётные записи»
    return RedirectResponse("/onboarding?step=2", status_code=303)


@router.post("/modules")
def set_modules(
    user_id: int = Form(...),
    module: str = Form(...),
    enabled: str = Form("off"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    target = db.get(User, user_id)
    if target is not None and target.family_id == current.family_id and target.is_member:
        set_module_enabled(db, user_id, module, enabled == "on")
    return RedirectResponse("/onboarding?step=3", status_code=303)


@router.post("/finish")
def finish():
    return RedirectResponse("/?welcome=1", status_code=303)
