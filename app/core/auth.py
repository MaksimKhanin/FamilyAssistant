"""Login with signed session cookies, plus the «acting as» switch.

The web panel lets a family member look at the panel through another member's
eyes (the avatar row in the header). It is a display convenience for a household
of five and nothing more: other people's nutrition figures stay hidden (see
`can_see_figures`), and nothing can be *changed* on someone else's behalf — since
the head of the family became a plain administrator (ADR-0008), acting for
another person has no owner, and `can_act_as` is identity only.

Администратора этот переключатель не касается вовсе: он не участник семьи, чужих
экранов у него нет, и смотреть ими он не может.
"""
from typing import Optional

import bcrypt
from fastapi import Depends, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.models import User

SESSION_COOKIE = "family_session"
SESSION_MAX_AGE_SEC = 60 * 60 * 24 * 30
ACTING_COOKIE = "family_acting_as"

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="family-assistant-session")


class NotAuthenticatedException(Exception):
    """No valid session — the app-level handler redirects to /login."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: Optional[str]) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def authenticate(db: Session, username: str, password: str) -> Optional[User]:
    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def _read_session(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE_SEC)
    except BadSignature:
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    data = _read_session(request)
    if not data:
        raise NotAuthenticatedException()
    user = db.get(User, data["uid"])
    if user is None:
        raise NotAuthenticatedException()
    return user


def session_user(request: Request, db: Session) -> Optional[User]:
    """Кто вошёл — по одной куке и одному запросу, без FastAPI-зависимостей.

    Нужно гейту ролей (`app/web/gate.py`): он работает раньше роутов, и `Depends`
    там взять неоткуда.
    """
    data = _read_session(request)
    return db.get(User, data["uid"]) if data else None


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    return session_user(request, db)


def get_viewed_user(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> User:
    """The family member whose screens are being shown (avatar row in the header)."""
    raw = request.cookies.get(ACTING_COOKIE)
    if not raw or not raw.isdigit() or current.is_admin:
        return current
    viewed = db.get(User, int(raw))
    # Админская учётка не «просматривается»: своих экранов у неё нет, и подставлять
    # её вместо участника значило бы показать пустоту вместо чужого дня.
    if viewed is None or viewed.family_id != current.family_id or viewed.is_admin:
        return current
    return viewed


def can_act_as(current: User, viewed: User) -> bool:
    """May `current` change data / run tools on behalf of `viewed`?

    Только сам за себя. Раньше это умел глава семьи, но роль главы разделилась
    на администратора и участника (ADR-0008), а у администратора нет ни разговора,
    ни модулей — значит, действовать за другого стало некому.
    """
    return current.id == viewed.id


def can_see_figures(current: User, viewed: User) -> bool:
    """Nutrition numbers are private: only the person themselves sees their calories.

    Остальные видят, что человек сегодня что-то записал и когда, но не сколько —
    это `showOthersCalories: false` из дизайн-пакета. Администратора вопрос не
    касается: экранов питания у него нет.
    """
    return current.id == viewed.id


def set_session_cookie(response, user: User):
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_serializer.dumps({"uid": user.id, "username": user.username}),
        max_age=SESSION_MAX_AGE_SEC,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(ACTING_COOKIE)
