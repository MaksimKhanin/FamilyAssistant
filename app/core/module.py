"""The module contract.

A module is a self-contained feature package under `app/modules/<name>/` that
exports a single `module: Module` object. The core assembles the application from
whatever modules are listed in `ENABLED_MODULES` and knows nothing about any of
them specifically. To add a module (finance, health, school, ...):

    1. create `app/modules/<name>/models.py` — tables always carrying user_id/family_id;
    2. create `app/modules/<name>/tools.py`  — @tool-decorated functions with clear signatures;
    3. create `app/modules/<name>/routes.py` — a FastAPI router for its web screens;
    4. export `module = Module(...)` from `__init__.py` and add the name to ENABLED_MODULES.

Everything else — per-user on/off flags, autonomy policy, tool traces, the chat,
the Telegram channel — comes for free.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter


@dataclass(frozen=True)
class NavItem:
    """One entry in the web panel's sidebar."""
    slug: str
    label: str
    url: str
    icon: str = "dot"
    group: str = ""                 # микро-заголовок группы: «Питание», «Безопасность», ...
    badge_key: Optional[str] = None  # ключ в контексте шаблона для счётчика-бейджа
    #: Флага «кому показывать» здесь нет намеренно: это решает адрес пункта и
    #: `app/core/roles.py`. Модуль, заведя экран настройки, называет его адрес
    #: там — иначе экран останется участниковым, как и всё по умолчанию.
    #: Короткая подпись для нижней панели на телефоне — там на пункт около 70px.
    short: Optional[str] = None

    @property
    def short_label(self) -> str:
        return self.short or self.label


@dataclass
class Module:
    name: str                       # технический идентификатор, он же ключ в module_access
    title: str                      # человекочитаемое название («Питание»)
    description: str                # одна строка для экранов профиля и онбординга
    routers: List[APIRouter] = field(default_factory=list)
    nav_items: List[NavItem] = field(default_factory=list)
    #: Пункты, которых нет в коде: их завёл сам человек (табло — экран одного
    #: показателя). `(db, user) -> List[NavItem]`; считается на каждый переход,
    #: поэтому запрос за ними обязан быть коротким.
    nav_items_for: Optional[Callable] = None
    #: Топики Event Bus → обработчики. Подписки регистрируются при сборке приложения.
    event_handlers: Dict[str, List[Callable]] = field(default_factory=dict)
    #: О чём человеку писать в памятке этой области — подсказка под полем на экране
    #: «Профиль и агент». Модуль без подсказки памятки не заводит: пустое поле без
    #: объяснения человек всё равно не заполнит. См. app/core/instructions.py.
    memo_hint: Optional[str] = None
    #: Личные данные (скоуп по user_id) или общие для семьи (скоуп по family_id).
    per_user: bool = True
    #: Модуль всегда включён и не показывается тумблером (например, «Память»).
    always_on: bool = False
