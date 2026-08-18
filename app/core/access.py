"""Module on/off flags — the entire access model of the MVP.

There are no roles beyond «администратор включает модуль участнику». Everything that
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


def enabled_user_ids(db: Session, module: str, default: bool = False) -> List[int]:
    """ID всех, кому включён этот модуль — обратная сторона `enabled_modules`.

    Для планировщика: перебирать всех `User` и звать `is_module_enabled` на
    каждого дороже, чем один запрос по `module_access`. `default` тот же, что
    у `is_module_enabled`: False — включён только тот, у кого есть явная
    строка `enabled=True` (для модулей, которые не должны включаться молча —
    см. модуль «Подход»); True — плюс те, у кого записи ещё нет вовсе.
    """
    rows = {r.user_id: r.enabled for r in db.query(ModuleAccess)
            .filter(ModuleAccess.module == module)}
    if not default:
        return [user_id for user_id, enabled in rows.items() if enabled]
    all_ids = [row[0] for row in db.query(User.id).all()]
    return [user_id for user_id in all_ids if rows.get(user_id, True)]


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
