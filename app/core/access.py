"""Module on/off flags — the entire access model of the MVP.

There are no roles beyond «глава семьи включает модуль участнику». Everything that
asks «may this person use module X?» goes through `is_module_enabled`, including
the agent layer, which hides a disabled module's tools from the LLM entirely
rather than letting it call them and fail.
"""
from typing import Dict, List

from sqlalchemy.orm import Session

from app.core.models import ModuleAccess, User


def is_module_enabled(db: Session, user_id: int, module: str, default: bool = True) -> bool:
    row = (
        db.query(ModuleAccess)
        .filter(ModuleAccess.user_id == user_id, ModuleAccess.module == module)
        .one_or_none()
    )
    return default if row is None else row.enabled


def enabled_modules(db: Session, user_id: int, known: List[str]) -> List[str]:
    rows = {r.module: r.enabled for r in db.query(ModuleAccess).filter(ModuleAccess.user_id == user_id)}
    return [name for name in known if rows.get(name, True)]


def set_module_enabled(db: Session, user_id: int, module: str, enabled: bool):
    row = (
        db.query(ModuleAccess)
        .filter(ModuleAccess.user_id == user_id, ModuleAccess.module == module)
        .one_or_none()
    )
    if row is None:
        row = ModuleAccess(user_id=user_id, module=module, enabled=enabled)
        db.add(row)
    else:
        row.enabled = enabled
    db.commit()


def access_matrix(db: Session, family_id: int, known: List[str]) -> Dict[int, Dict[str, bool]]:
    """{user_id: {module: enabled}} for the whole family — used by the onboarding matrix."""
    members = db.query(User).filter(User.family_id == family_id).all()
    rows = (
        db.query(ModuleAccess)
        .filter(ModuleAccess.user_id.in_([m.id for m in members]))
        .all()
    ) if members else []
    by_user: Dict[int, Dict[str, bool]] = {m.id: {name: True for name in known} for m in members}
    for row in rows:
        if row.user_id in by_user and row.module in by_user[row.user_id]:
            by_user[row.user_id][row.module] = row.enabled
    return by_user
