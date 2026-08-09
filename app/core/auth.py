"""Login with signed session cookies, plus the «acting as» switch.

The web panel lets whoever is logged in look at the panel through another family
member's eyes (the avatar row in the header). That is a display convenience for a
household of five, not a privilege escalation path: other people's nutrition
figures stay hidden (see `can_see_figures`), and anything the agent *does* is done
on behalf of the selected member only when the logged-in user is the head of the
family or the member themselves.
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


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    data = _read_session(request)
    return db.get(User, data["uid"]) if data else None


def get_viewed_user(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
) -> User:
    """The family member whose screens are being shown (avatar row in the header)."""
    raw = request.cookies.get(ACTING_COOKIE)
    if not raw or not raw.isdigit():
        return current
    viewed = db.get(User, int(raw))
    if viewed is None or viewed.family_id != current.family_id:
        return current
    return viewed


def can_act_as(current: User, viewed: User) -> bool:
    """May `current` change data / run tools on behalf of `viewed`?"""
    return current.id == viewed.id or current.is_head


def can_see_figures(current: User, viewed: User) -> bool:
    """Nutrition numbers are private: only the person themselves sees their calories.

    The head of the family sees who logged something and when, but not the figures —
    this mirrors the `showOthersCalories: false` default in the design package.
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
