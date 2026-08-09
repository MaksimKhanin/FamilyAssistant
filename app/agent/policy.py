"""Who may the agent be, and how independent — resolved per user, per tool.

Two dials, in this order of precedence:

  1. an explicit per-tool override on the «Агент и инструменты» screen
     (Сам / Спросит / Выкл) — always wins;
  2. the autonomy slider (0..3): a tool runs by itself when the slider has reached
     the tool's own `auto_from` level, otherwise it prepares the action and asks.

On top of that sits the module flag: a tool of a module that is switched off for
this person is not merely refused — it is never shown to the model at all, so the
assistant does not offer things the family did not turn on.
"""
from typing import List

from sqlalchemy.orm import Session

from app.core.access import is_module_enabled
from app.core.models import MODE_ASK, MODE_AUTO, MODE_OFF, ToolPolicy, User
from app.agent import registry
from app.agent.registry import ToolSpec


def resolve_mode(db: Session, user: User, spec: ToolSpec) -> str:
    """Return MODE_AUTO / MODE_ASK / MODE_OFF for this user and tool."""
    override = (
        db.query(ToolPolicy)
        .filter(ToolPolicy.user_id == user.id, ToolPolicy.tool == spec.name)
        .one_or_none()
    )
    if override is not None:
        return override.mode
    return MODE_AUTO if (user.autonomy or 0) >= spec.auto_from else MODE_ASK


def set_mode(db: Session, user: User, tool_name: str, mode: str):
    if mode not in (MODE_AUTO, MODE_ASK, MODE_OFF):
        raise ValueError(f"Неизвестный режим инструмента: {mode}")
    row = (
        db.query(ToolPolicy)
        .filter(ToolPolicy.user_id == user.id, ToolPolicy.tool == tool_name)
        .one_or_none()
    )
    if row is None:
        db.add(ToolPolicy(user_id=user.id, tool=tool_name, mode=mode))
    else:
        row.mode = mode
    db.commit()


def available_tools(db: Session, user: User, include_internal: bool = False) -> List[ToolSpec]:
    """Tools this person's assistant is allowed to consider at all."""
    result = []
    for spec in registry.all_specs(include_internal=include_internal):
        if not is_module_enabled(db, user.id, spec.module):
            continue
        if resolve_mode(db, user, spec) == MODE_OFF:
            continue
        result.append(spec)
    return result


def policy_overview(db: Session, user: User) -> List[dict]:
    """Rows for the «Агент и инструменты» screen: every tool with its effective mode."""
    rows = []
    for spec in registry.all_specs(include_internal=True):
        rows.append({
            "spec": spec,
            "mode": resolve_mode(db, user, spec),
            "module_enabled": is_module_enabled(db, user.id, spec.module),
        })
    return rows
