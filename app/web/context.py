"""Everything every screen needs: navigation, the family avatar row, badges, theme.

One builder so the shell stays identical across core screens and module screens,
and so a new module gets the whole chrome for free by contributing NavItems.
"""
from typing import Dict, List, Set

from fastapi import Request
from sqlalchemy.orm import Session

from app.core import family as family_service
from app.core.access import enabled_modules
from app.core.auth import can_act_as, can_see_figures
from app.core.module import NavItem
from app.core.models import User
from app.modules import load_modules

#: Avatar palettes (фон / текст) for up to five members, from the design package.
AVATAR_PALETTE = [
    ("#F0D9CF", "#A34A28"),
    ("#DCE6D8", "#4F6A4A"),
    ("#F3E3C4", "#96690F"),
    ("#DDE2EC", "#4A5A78"),
    ("#EADFE9", "#75517A"),
]

GROUP_ORDER = ["", "Питание", "Безопасность", "Настройки", "Администрирование"]

CORE_NAV = [
    NavItem(slug="dashboard", label="Главная", url="/", icon="grid"),
]

SETTINGS_NAV = [
    NavItem(slug="connectors", label="Коннекторы", url="/settings/connectors", icon="plug", group="Настройки"),
    NavItem(slug="agent", label="Агент и инструменты", url="/settings/agent", icon="wand", group="Настройки"),
    NavItem(slug="family", label="Семья и модули", url="/settings/family", icon="users", group="Настройки"),
    NavItem(slug="profile", label="Профиль", url="/settings/profile", icon="user", group="Настройки"),
    NavItem(slug="llm", label="Модель и знания", url="/settings/model", icon="brain",
            group="Администрирование", head_only=True),
    NavItem(slug="traces", label="Трейсы агента", url="/settings/traces", icon="pulse",
            group="Администрирование", head_only=True),
]


def avatar(user: User) -> Dict[str, str]:
    background, color = AVATAR_PALETTE[(user.avatar_slot or 0) % len(AVATAR_PALETTE)]
    return {
        "id": user.id,
        "initial": (user.display_name or "?")[:1].upper(),
        "name": user.display_name,
        "relation": user.relation or "",
        "bg": background,
        "fg": color,
    }


def build_nav(enabled: Set[str], current: User) -> List[dict]:
    """Sidebar groups, with items of switched-off modules left out.

    `enabled` считается один раз в `screen_context` — навигация строится на
    каждый переход, и ходить в базу за каждым модулем отдельно было бы дорого.
    """
    items: List[NavItem] = list(CORE_NAV)
    for module in load_modules():
        if module.always_on or module.name in enabled:
            items.extend(module.nav_items)
    items.extend(SETTINGS_NAV)

    groups: Dict[str, List[NavItem]] = {}
    for item in items:
        if item.head_only and not current.is_head:
            continue
        groups.setdefault(item.group, []).append(item)

    ordered = [g for g in GROUP_ORDER if g in groups]
    ordered += [g for g in groups if g not in GROUP_ORDER]
    # ключ «entries», а не «items»: в Jinja у dict уже есть метод .items
    return [{"title": group, "entries": groups[group]} for group in ordered]


#: Что попадает в нижнюю панель на телефоне — по одному пункту на то, ради чего
#: приложение вообще открывают. Остальное прячется в выдвижное меню.
QUICK_NAV = [
    ("dashboard", None),
    ("meal", "nutrition"),
    ("events", "security"),
    ("memory", None),
]


def build_quick_nav(nav_groups: List[dict]) -> List[NavItem]:
    by_slug = {item.slug: item for group in nav_groups for item in group["entries"]}
    return [by_slug[slug] for slug, _ in QUICK_NAV if slug in by_slug]


def badges(db: Session, viewed: User, enabled: Set[str]) -> Dict[str, int]:
    """Counters the sidebar shows next to nav items."""
    result: Dict[str, int] = {}
    if "security" in enabled:
        from app.modules.security import service as security_service
        result["anomaly_count"] = security_service.anomaly_count(db, viewed.family_id, days=1)
    return result


def screen_context(request: Request, db: Session, current: User, viewed: User,
                   title: str, subtitle: str = "") -> dict:
    members = family_service.members(db, viewed.family_id)
    settings_row = family_service.get_settings(db, viewed.family_id)

    # Один запрос на все флаги модулей: навигация, бейджи и сами экраны дальше
    # смотрят в это множество, а не дёргают is_module_enabled по одному.
    enabled = set(enabled_modules(db, viewed.id, [m.name for m in load_modules()]))
    nav_groups = build_nav(enabled, current)

    return {
        "request": request,
        "title": title,
        "subtitle": subtitle,
        "current_user": current,
        "viewed_user": viewed,
        "family": current.family,
        "family_settings": settings_row,
        "accent": settings_row.accent_color,
        "members": members,
        "avatars": [avatar(m) for m in members],
        "viewed_avatar": avatar(viewed),
        "nav_groups": nav_groups,
        "quick_nav": build_quick_nav(nav_groups),
        "badges": badges(db, viewed, enabled),
        "enabled_modules": enabled,
        "active_path": request.url.path,
        "can_edit": can_act_as(current, viewed),
        "can_see_figures": can_see_figures(current, viewed),
        "modules": load_modules(),
    }
