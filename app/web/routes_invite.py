"""Приглашение участника по одноразовой ссылке.

Панель — единственный вход в ассистента, поэтому добавленный человек должен уметь
завести себе пароль сам, не дожидаясь, пока администратор что-то ему пропишет.
Глава добавляет участника и передаёт ссылку любым способом; ссылка одноразовая и
сгорает, как только пароль задан.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.accounts import find_by_invite, new_invite_code  # noqa: F401  (реэкспорт для роутов)
from app.core.auth import hash_password, set_session_cookie
from app.core.config import settings
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render

router = APIRouter(tags=["invite"])

MIN_PASSWORD_LENGTH = 6


def invite_url(user: User, request: Request = None) -> str:
    """Ссылка, по которой человек заведёт себе пароль.

    Адрес берётся из запроса, а не из `PUBLIC_BASE_URL`: администратор смотрит
    панель по тому же адресу, по которому её откроет приглашённый — они в одном
    доме. У переменной окружения умолчание «localhost», и с ним ссылка
    собиралась без ошибок, но не открывалась ни с одного другого устройства:
    самая частая поломка приглашений и была в этом.

    Явно заданный `PUBLIC_BASE_URL` всё равно главнее: если панель стоит за
    доменом, а внутрь ходит по служебному адресу, знает об этом только тот, кто
    её разворачивал.
    """
    if not user.invite_code:
        return ""
    base = settings.public_base_url if settings.public_base_url_explicit or request is None \
        else str(request.base_url)
    return f"{base.rstrip('/')}/invite/{user.invite_code}"


def _find(db: Session, code: str) -> User:
    return find_by_invite(db, code)


@router.get("/invite/{code}", response_class=HTMLResponse)
def invite_form(code: str, request: Request, db: Session = Depends(get_db)):
    user = _find(db, code)
    if user is None:
        return render(request, "invite_expired.html", {"request": request}, status_code=404)
    return render(request, "invite.html",
                  {"request": request, "invited": user, "code": code, "error": None})


@router.post("/invite/{code}", response_class=HTMLResponse)
def accept_invite(
    code: str,
    request: Request,
    password: str = Form(...),
    password_repeat: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _find(db, code)
    if user is None:
        return render(request, "invite_expired.html", {"request": request}, status_code=404)

    error = None
    if len(password) < MIN_PASSWORD_LENGTH:
        error = f"Пароль короче {MIN_PASSWORD_LENGTH} символов — придумайте подлиннее"
    elif password != password_repeat:
        error = "Пароли не совпали"

    if error:
        return render(request, "invite.html",
                      {"request": request, "invited": user, "code": code, "error": error},
                      status_code=400)

    user.password_hash = hash_password(password)
    user.invite_code = None          # ссылка одноразовая
    db.commit()

    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, user)
    return response
