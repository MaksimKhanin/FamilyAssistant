"""Что ассистенту позволено делать — и насколько без спроса.

Ручек по-прежнему две, но у каждой два слоя — общий и личный:

  1. режим инструмента (Сам / Спросит / Выкл): сначала личный, потом семейный;
  2. самостоятельность (0..3): личная, если человек её задал, иначе семейная.
     Инструмент выполняется сам, когда она дотянулась до его `auto_from`.

Общий слой задаёт администратор на экране «Агент и инструменты», личный —
человек себе на «Профиле и агенте» или словами в разговоре, и личное перебивает
общее (ADR-0012). Порядок между слоями внутри одного инструмента такой: своё
исключение → исключение дома → самостоятельность. Более точная настройка бьёт
более общую, чья бы она ни была, — иначе личный ползунок молча отменял бы
исключение, выставленное на весь дом руками.

Исключение из «личное перебивает общее» ровно одно: `MODE_OFF`, выставленный
администратором. Выключенный инструмент — единственный настоящий запрет в доме,
а не пожелание об удобстве; личной настройкой он не включается, и ассистент по
просьбе человека тоже его не включит.

On top of that sits the module flag: a tool of a module that is switched off for
this person is not merely refused — it is never shown to the model at all, so the
assistant does not offer things the family did not turn on. Он тоже админский:
модули включают каждому свои, и сам себе человек модуль не открывает.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.access import is_module_enabled
from app.core.family import get_settings
from app.core.models import (
    MODE_ASK, MODE_AUTO, MODE_OFF, ToolPolicy, User, UserToolPolicy,
)
from app.agent import registry
from app.agent.registry import ToolSpec

MODES = (MODE_AUTO, MODE_ASK, MODE_OFF)

#: Откуда взялся режим инструмента — для экранов и для объяснений ассистента.
FROM_FAMILY_OFF = "family-off"   # выключено администратором на весь дом
FROM_OWN = "own"                 # личное исключение человека
FROM_FAMILY = "family"           # исключение, выставленное на весь дом
FROM_AUTONOMY = "autonomy"       # никаких исключений — работает самостоятельность


class LockedByFamily(Exception):
    """Инструмент выключен администратором: личной настройкой его не вернуть."""


@dataclass(frozen=True)
class Dials:
    """Обе ручки в обоих слоях — всё, что нужно, чтобы решить про любой инструмент.

    Собирается парой запросов на разговор, а не по запросу на инструмент:
    инструментов три десятка, и `resolve_mode` в цикле означал бы три десятка
    походов в базу за одним и тем же.
    """
    family_autonomy: int
    family_modes: Dict[str, str] = field(default_factory=dict)
    own_autonomy: Optional[int] = None
    own_modes: Dict[str, str] = field(default_factory=dict)

    @property
    def autonomy(self) -> int:
        """Та самостоятельность, по которой ассистент и будет работать."""
        return self.family_autonomy if self.own_autonomy is None else self.own_autonomy

    @property
    def follows_family(self) -> bool:
        """Своей самостоятельности человек не задавал — идёт за домом."""
        return self.own_autonomy is None

    def source(self, spec: ToolSpec) -> str:
        if self.family_modes.get(spec.name) == MODE_OFF:
            return FROM_FAMILY_OFF
        if spec.name in self.own_modes:
            return FROM_OWN
        if spec.name in self.family_modes:
            return FROM_FAMILY
        return FROM_AUTONOMY

    def mode(self, spec: ToolSpec) -> str:
        origin = self.source(spec)
        if origin == FROM_FAMILY_OFF:
            return MODE_OFF
        if origin == FROM_OWN:
            return self.own_modes[spec.name]
        if origin == FROM_FAMILY:
            return self.family_modes[spec.name]
        return MODE_AUTO if self.autonomy >= spec.auto_from else MODE_ASK


def family_dials(db: Session, family_id: int) -> Dials:
    """Дом без личного слоя — то, что видит и крутит администратор."""
    return Dials(
        family_autonomy=get_settings(db, family_id).autonomy or 0,
        family_modes={
            row.tool: row.mode
            for row in db.query(ToolPolicy).filter(ToolPolicy.family_id == family_id)
        },
    )


def dials(db: Session, user: User) -> Dials:
    """Обе ручки глазами одного человека: дом плюс то, что он поправил себе."""
    shared = family_dials(db, user.family_id)
    return Dials(
        family_autonomy=shared.family_autonomy,
        family_modes=shared.family_modes,
        own_autonomy=user.autonomy,
        own_modes={
            row.tool: row.mode
            for row in db.query(UserToolPolicy).filter(UserToolPolicy.user_id == user.id)
        },
    )


def resolve_mode(db: Session, user: User, spec: ToolSpec) -> str:
    """Return MODE_AUTO / MODE_ASK / MODE_OFF for this tool for this person."""
    return dials(db, user).mode(spec)


# --- что крутит администратор (на весь дом) -------------------------------

def set_mode(db: Session, family_id: int, tool_name: str, mode: str):
    if mode not in MODES:
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
    """Общая самостоятельность — умолчание дома для всех, кто не задал своей."""
    settings_row = get_settings(db, family_id)
    settings_row.autonomy = max(0, min(3, int(level)))
    db.commit()
    return settings_row.autonomy


# --- что крутит себе человек ----------------------------------------------

def set_own_autonomy(db: Session, user: User, level: Optional[int]) -> Optional[int]:
    """Своя самостоятельность. `None` — снять её и снова идти за домом."""
    user.autonomy = None if level is None else max(0, min(3, int(level)))
    db.commit()
    return user.autonomy


def set_own_mode(db: Session, user: User, tool_name: str, mode: Optional[str]):
    """Личное исключение по инструменту. `None` — снять его и вернуться к дому.

    Выключенное администратором не включается: `LockedByFamily` вместо тихого
    сохранения строки, которая всё равно ничего не изменила бы, — человеку надо
    сказать, почему инструмента у него нет, а не сделать вид, что включили.
    """
    if mode is not None and mode not in MODES:
        raise ValueError(f"Неизвестный режим инструмента: {mode}")
    if registry.get(tool_name) is None:
        raise ValueError(f"Неизвестный инструмент: {tool_name}")
    if family_dials(db, user.family_id).family_modes.get(tool_name) == MODE_OFF:
        raise LockedByFamily(tool_name)

    row = (
        db.query(UserToolPolicy)
        .filter(UserToolPolicy.user_id == user.id, UserToolPolicy.tool == tool_name)
        .one_or_none()
    )
    if mode is None:
        # Пустой строки не держим: «как в доме» — это отсутствие исключения, и
        # тогда человек продолжает следовать за домом, когда тот поменяется.
        if row is not None:
            db.delete(row)
            db.commit()
        return None
    if row is None:
        db.add(UserToolPolicy(user_id=user.id, tool=tool_name, mode=mode))
    else:
        row.mode = mode
    db.commit()
    return mode


# --- списки ---------------------------------------------------------------

def available_tools(db: Session, user: User, include_internal: bool = False) -> List[ToolSpec]:
    """Tools this person's assistant is allowed to consider at all."""
    resolved = dials(db, user)
    result = []
    for spec in registry.all_specs(include_internal=include_internal):
        if spec.available is not None and not spec.available():
            continue
        if not is_module_enabled(db, user.id, spec.module):
            continue
        if resolved.mode(spec) == MODE_OFF:
            continue
        result.append(spec)
    return result


def policy_overview(db: Session, family_id: int) -> List[dict]:
    """Rows for the «Агент и инструменты» screen: every tool with its house mode."""
    resolved = family_dials(db, family_id)
    return [{"spec": spec, "mode": resolved.mode(spec)}
            for spec in registry.all_specs(include_internal=True)]


def own_overview(db: Session, user: User) -> List[dict]:
    """Rows for «Профиль и агент»: инструменты этого человека и чем задан режим.

    Внутренних тут нет и выключенных модулей тоже: экран показывает то, чем
    ассистент с этим человеком пользуется, а не весь реестр — иначе человек
    крутил бы режим разбору кадра с камеры, которого у него нет.
    """
    resolved = dials(db, user)
    rows = []
    for spec in registry.all_specs():
        if not is_module_enabled(db, user.id, spec.module):
            continue
        origin = resolved.source(spec)
        if origin == FROM_FAMILY_OFF:
            continue
        rows.append({
            "spec": spec,
            "mode": resolved.mode(spec),
            "own": resolved.own_modes.get(spec.name),
            "source": origin,
        })
    return rows


def own_exceptions(db: Session, user: User) -> List[dict]:
    """Только личные исключения — для промпта и для карточки в профиле."""
    return [row for row in own_overview(db, user) if row["own"] is not None]
