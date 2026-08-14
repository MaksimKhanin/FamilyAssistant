"""Что ассистенту позволено делать — и насколько без спроса.

Two dials, in this order of precedence:

  1. an explicit per-tool override on the «Агент и инструменты» screen
     (Сам / Спросит / Выкл) — always wins;
  2. the autonomy slider (0..3): a tool runs by itself when the slider has reached
     the tool's own `auto_from` level, otherwise it prepares the action and asks.

Обе — семейные: их задаёт администратор один раз для всех (ADR-0007). Личным
здесь не остаётся ничего, и это сознательно: «сам делает рутину» — это про
доверие к ассистенту в доме, а не про настроение отдельного человека.

On top of that sits the module flag: a tool of a module that is switched off for
this person is not merely refused — it is never shown to the model at all, so the
assistant does not offer things the family did not turn on. Вот он как раз
личный: модули включают каждому свои.
"""
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

from app.core.access import is_module_enabled
from app.core.family import get_settings
from app.core.models import MODE_ASK, MODE_AUTO, MODE_OFF, ToolPolicy, User
from app.agent import registry
from app.agent.registry import ToolSpec


def dials(db: Session, family_id: int) -> Tuple[int, Dict[str, str]]:
    """Самостоятельность и все исключения семьи — двумя запросами, а не по одному на инструмент."""
    autonomy = get_settings(db, family_id).autonomy or 0
    overrides = {
        row.tool: row.mode
        for row in db.query(ToolPolicy).filter(ToolPolicy.family_id == family_id)
    }
    return autonomy, overrides


def mode_for(spec: ToolSpec, autonomy: int, overrides: Dict[str, str]) -> str:
    if spec.name in overrides:
        return overrides[spec.name]
    return MODE_AUTO if autonomy >= spec.auto_from else MODE_ASK


def resolve_mode(db: Session, user: User, spec: ToolSpec) -> str:
    """Return MODE_AUTO / MODE_ASK / MODE_OFF for this tool in this family."""
    autonomy, overrides = dials(db, user.family_id)
    return mode_for(spec, autonomy, overrides)


def set_mode(db: Session, family_id: int, tool_name: str, mode: str):
    if mode not in (MODE_AUTO, MODE_ASK, MODE_OFF):
        raise ValueError(f"Неизвестный режим инструмента: {mode}")
    row = (
        db.query(ToolPolicy)
        .filter(ToolPolicy.family_id == family_id, ToolPolicy.tool == tool_name)
        .one_or_none()
    )
    if row is None:
        db.add(ToolPolicy(family_id=family_id, tool=tool_name, mode=mode))
    else:
        row.mode = mode
    db.commit()


def set_autonomy(db: Session, family_id: int, level: int):
    """Ползунок самостоятельности — один на семью."""
    settings_row = get_settings(db, family_id)
    settings_row.autonomy = max(0, min(3, int(level)))
    db.commit()
    return settings_row.autonomy


def available_tools(db: Session, user: User, include_internal: bool = False) -> List[ToolSpec]:
    """Tools this person's assistant is allowed to consider at all."""
    autonomy, overrides = dials(db, user.family_id)
    result = []
    for spec in registry.all_specs(include_internal=include_internal):
        if not is_module_enabled(db, user.id, spec.module):
            continue
        if mode_for(spec, autonomy, overrides) == MODE_OFF:
            continue
        result.append(spec)
    return result


def policy_overview(db: Session, family_id: int) -> List[dict]:
    """Rows for the «Агент и инструменты» screen: every tool with its effective mode."""
    autonomy, overrides = dials(db, family_id)
    return [{"spec": spec, "mode": mode_for(spec, autonomy, overrides)}
            for spec in registry.all_specs(include_internal=True)]
