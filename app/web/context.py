"""Everything every screen needs: navigation, the family avatar row, badges, theme.

One builder so the shell stays identical across core screens and module screens,
and so a new module gets the whole chrome for free by contributing NavItems.
"""
from typing import Dict, List, Optional, Set

from fastapi import Request
from sqlalchemy.orm import Session

from app.core import family as family_service
from app.core.access import enabled_modules
from app.core.auth import can_act_as, can_see_figures
from app.core.module import NavItem
from app.core.models import THEME_WARM, THEMES, User
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

#: Акцент, с которым семья заводится, — цвет прежнего оформления. Пока его никто
#: не трогал, акцент берётся из темы (терракота днём, янтарь ночью); как только
#: семья выберет свой, он перебьёт тему в обоих оформлениях. Так «акцент семьи»
#: остаётся её настройкой, а не молчаливым наследством одного из оформлений.
DEFAULT_ACCENT = "#2E6E7E"

CORE_NAV = [
    NavItem(slug="dashboard", label="Главная", url="/", icon="grid"),
]

SETTINGS_NAV = [
    NavItem(slug="connectors", label="Коннекторы", url="/settings/connectors", icon="plug", group="Настройки"),
    # Профиль и настройки агента — один экран: на телефоне их разделение
    # заставляло ходить туда-обратно между «кто я» и «что ему можно».
    NavItem(slug="profile", label="Профиль и агент", url="/settings/profile", icon="user", group="Настройки"),
    NavItem(slug="family", label="Семья и модули", url="/settings/family", icon="users", group="Настройки"),
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


def build_nav(db: Session, enabled: Set[str], current: User) -> List[dict]:
    """Sidebar groups, with items of switched-off modules left out.

    `enabled` считается один раз в `screen_context` — навигация строится на
    каждый переход, и ходить в базу за каждым модулем отдельно было бы дорого.

    Кроме статических пунктов модуль отдаёт и заведённые самим человеком
    (`nav_items_for`) — сегодня это табло. Спрашивают их про `current`, а не
    `viewed`: знания и всё, что из них растёт, исключены из режима «от лица»
    (ADR-0005), и переключение аватара в шапке чужих табло не показывает.
    """
    items: List[NavItem] = list(CORE_NAV)
    for module in load_modules():
        if not (module.always_on or module.name in enabled):
            continue
        items.extend(module.nav_items)
        if module.nav_items_for is not None:
            items.extend(module.nav_items_for(db, current))
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


#: Что попадает в нижнюю панель на телефоне. Пунктов два, и оба по краям:
#: посередине всегда стоит «Разговор», и он не отсюда — разговор есть всегда,
#: даже когда оба модуля выключены, и в NavItem его нет. Пять пунктов, как было
#: раньше, не помещались подписями, а четыре из них открывали то, о чём проще
#: спросить словами. Остальное живёт в выдвижном меню и в сайдбаре компьютера.
QUICK_NAV = [
    ("events", "security"),
    ("memory", None),
]


def build_quick_nav(nav_groups: List[dict]) -> List[Optional[NavItem]]:
    """По одному месту на пункт — даже если пункта нет.

    Длина списка постоянна: выключенный модуль отдаёт `None`, и «Разговор»
    посередине остаётся посередине, а не съезжает к краю.
    """
    by_slug = {item.slug: item for group in nav_groups for item in group["entries"]}
    return [by_slug.get(slug) for slug, _ in QUICK_NAV]


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
    nav_groups = build_nav(db, enabled, current)

    return {
        "request": request,
        "title": title,
        "subtitle": subtitle,
        "current_user": current,
        "viewed_user": viewed,
        "family": current.family,
        "family_settings": settings_row,
        "accent": None if settings_row.accent_color == DEFAULT_ACCENT else settings_row.accent_color,
        # Оформление берётся у того, кто смотрит, а не у того, от чьего лица:
        # режим «от лица» меняет данные экрана, а не глаза человека перед ним.
        "theme": current.theme if current.theme in THEMES else THEME_WARM,
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
