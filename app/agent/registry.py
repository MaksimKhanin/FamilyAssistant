"""Tool registry — the contract between modules and the agent.

A module exposes its capabilities as `@tool`-decorated functions. Each one declares
a JSON-Schema signature the LLM sees, the module it belongs to (so it disappears
for people who have that module switched off), and how independent the agent may
be with it (`auto_from`).

Nothing here talks to the LLM: the registry only describes tools and runs them.
"""
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.models import User

logger = get_logger("tools")


@dataclass
class ToolContext:
    """Everything a tool needs to know about *who* it is acting for.

    `attachments` carries what came with the message but cannot travel through a
    JSON tool signature — today that is the meal photo (`image`, `image_mime`).
    The model asks for `log_meal`; the bytes reach the tool through here.
    """
    db: Session
    actor: User            # кто разговаривает с агентом
    subject: User          # чьи данные меняем (обычно тот же человек)
    channel: str = "web"   # web|telegram|schedule|event
    attachments: Dict[str, Any] = field(default_factory=dict)

    @property
    def family_id(self) -> int:
        return self.subject.family_id


@dataclass
class ToolResult:
    """What a tool gives back.

    `summary` goes to the LLM as the tool result and to the action log; `card` is
    an optional structured block the UI renders under the agent's reply (meal /
    stats / security / plan / memory — see docs/agent.md).
    """
    summary: str
    data: Dict[str, Any] = field(default_factory=dict)
    card: Optional[Dict[str, Any]] = None
    ok: bool = True


@dataclass
class ToolSpec:
    name: str
    module: str
    title: str                       # человекочитаемое имя для экрана «Агент и инструменты»
    description: str                 # то, что видит модель
    parameters: Dict[str, Any]       # JSON Schema
    handler: Callable[..., ToolResult]
    #: Минимальный уровень автономности (0..3), с которого инструмент выполняется без вопроса.
    #: 0 — только чтение, спрашивать не о чем; 3 — действие наружу, по умолчанию всегда спросит.
    auto_from: int = 2
    read_only: bool = False
    #: Инструмент нельзя вызвать из чата напрямую — только по событию/расписанию.
    internal: bool = False


REGISTRY: Dict[str, ToolSpec] = {}


def tool(
    name: str,
    module: str,
    title: str,
    description: str,
    parameters: Dict[str, Any] = None,
    auto_from: int = 2,
    read_only: bool = False,
    internal: bool = False,
):
    """Register a function as an agent tool.

    The handler is always called as `handler(ctx, **arguments)`.
    """
    def decorator(fn: Callable[..., ToolResult]):
        if name in REGISTRY:
            raise RuntimeError(f"Инструмент {name} уже зарегистрирован")
        REGISTRY[name] = ToolSpec(
            name=name,
            module=module,
            title=title,
            description=inspect.cleandoc(description),
            parameters=parameters or {"type": "object", "properties": {}},
            handler=fn,
            auto_from=0 if read_only else auto_from,
            read_only=read_only,
            internal=internal,
        )
        return fn

    return decorator


def get(name: str) -> Optional[ToolSpec]:
    return REGISTRY.get(name)


def all_specs(include_internal: bool = False) -> List[ToolSpec]:
    return [s for s in REGISTRY.values() if include_internal or not s.internal]


def openai_schema(spec: ToolSpec) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def execute(spec: ToolSpec, ctx: ToolContext, arguments: Dict[str, Any]) -> ToolResult:
    """Run a tool, keeping a bad call from taking the conversation down with it."""
    accepted = set((spec.parameters.get("properties") or {}).keys())
    unknown = set(arguments) - accepted
    if unknown:
        logger.warning(f"{spec.name}: модель прислала лишние аргументы {sorted(unknown)} — игнорирую")
    clean = {k: v for k, v in arguments.items() if k in accepted}

    missing = [k for k in spec.parameters.get("required", []) if k not in clean]
    if missing:
        return ToolResult(summary=f"Не хватает данных: {', '.join(missing)}", ok=False)

    try:
        return spec.handler(ctx, **clean)
    except Exception as e:
        logger.exception(f"Инструмент {spec.name} упал")
        return ToolResult(summary=f"Не получилось выполнить: {e}", ok=False)
