"""Login, logout, and the «смотрю глазами другого участника» switch."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import (
    ACTING_COOKIE, authenticate, clear_session_cookie, get_current_user, set_session_cookie,
)
from app.core.config import settings
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = authenticate(db, username.strip(), password)
    if user is None:
        return render(request, 
            "login.html",
            {"request": request, "error": "Не узнаю эту пару логина и пароля"},
            status_code=401,
        )
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, user)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response)
    return response


@router.post("/switch-member/{user_id}")
def switch_member(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Avatar row in the header — look at the panel as another family member.

    Only within one's own family, and figures stay private regardless (see
    core.auth.can_see_figures).
    """
    target = db.get(User, user_id)
    back = request.headers.get("referer") or "/"
    response = RedirectResponse(back, status_code=303)
    if target is not None and target.family_id == current.family_id:
        response.set_cookie(ACTING_COOKIE, str(target.id), httponly=True,
                            secure=settings.cookie_secure, samesite="lax")
    return response
