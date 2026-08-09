"""Module loader.

The core imports whatever `ENABLED_MODULES` lists and asks each package for its
`module: Module` object. Importing the package is also what registers its tables
(so `create_all` sees them) and its agent tools (so the registry knows them).
"""
import importlib
from typing import Dict, List

from app.core.config import ENABLED_MODULES
from app.core.events import bus
from app.core.logging import get_logger
from app.core.module import Module

logger = get_logger("modules")

_loaded: List[Module] = []


def load_modules() -> List[Module]:
    """Import and return the enabled modules. Idempotent."""
    if _loaded:
        return _loaded

    for name in ENABLED_MODULES:
        try:
            package = importlib.import_module(f"app.modules.{name}")
        except ImportError:
            logger.exception(f"Модуль «{name}» не удалось загрузить — пропускаю")
            continue

        module = getattr(package, "module", None)
        if not isinstance(module, Module):
            logger.error(f"Пакет app.modules.{name} не экспортирует объект Module — пропускаю")
            continue

        for topic, handlers in module.event_handlers.items():
            for handler in handlers:
                bus.subscribe(topic, handler)

        _loaded.append(module)
        logger.info(f"Загружен модуль: {module.title} ({module.name})")

    return _loaded


def by_name() -> Dict[str, Module]:
    return {m.name: m for m in load_modules()}


def togglable() -> List[Module]:
    """Modules the head of the family switches on and off per person."""
    return [m for m in load_modules() if not m.always_on]


def names() -> List[str]:
    return [m.name for m in load_modules()]
