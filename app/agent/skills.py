"""Скиллы — подгружаемые инструкции для особых ситуаций (docs/skills.md §7).

Скилл — это markdown-файл `skills/<имя>/SKILL.md` с фронтматтером из двух-трёх
строк. В контексте модели всегда висят только заголовки (имя и описание — в
описании инструмента `load_skill`); тело читается инструментом уже после того,
как модель сама решила, что ситуация особая. Так постоянный промпт-налог не
растёт с числом инструкций: расти может каталог, а не каждый ход.

Скилл в этой архитектуре — только текст: ни скриптов, ни команд (docs/skills.md
§9). Фильтр по модулям человека — в обработчике: перечень имён в схеме один на
всех, а чужая инструкция честно отвечает отказом.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from app.core.logging import get_logger

logger = get_logger("skills")

#: Каталог скиллов ассистента. Не `.claude/skills/` — там инструкции для
#: агента, который пишет код, а это разные наборы с разной судьбой.
SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"

#: Сколько знаков тела доезжает до модели. Инструкция длиннее — не инструкция,
#: а документация: её место в docs/.
BODY_LIMIT = 4000

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    #: Модуль, при выключенном котором инструкция человеку недоступна.
    #: Пустая строка — инструкция общая.
    module: str
    path: Path


_index: Dict[str, Skill] = {}
_registered = False


def load_index(root: Path = None) -> Dict[str, Skill]:
    """Разобрать фронтматтеры один раз при подъёме приложения. Тела не читать."""
    root = root or SKILLS_DIR
    found: Dict[str, Skill] = {}
    if not root.is_dir():
        return found
    for skill_file in sorted(root.glob("*/SKILL.md")):
        meta = _frontmatter(skill_file)
        name = meta.get("name") or skill_file.parent.name
        description = meta.get("description", "").strip()
        if not description:
            logger.warning(f"Скилл {skill_file} без description — пропускаю: "
                           f"без описания модели не из чего выбирать")
            continue
        found[name] = Skill(name=name, description=description,
                            module=meta.get("module", "").strip(), path=skill_file)
    return found


def _frontmatter(path: Path) -> Dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(f"Скилл {path} не читается — пропускаю")
        return {}
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    meta = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        if _:
            meta[key.strip()] = value.strip()
    return meta


def index() -> Dict[str, Skill]:
    return dict(_index)


def get(name: str) -> Optional[Skill]:
    return _index.get(name)


def body(name: str) -> str:
    """Тело инструкции — без фронтматтера, с потолком по длине."""
    skill = get(name)
    if skill is None:
        return ""
    try:
        text = skill.path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return _FRONTMATTER.sub("", text).strip()[:BODY_LIMIT]


def register(root: Path = None):
    """Собрать индекс и зарегистрировать инструмент. Идемпотентно.

    Зовётся из `load_modules()`: скиллы — часть сборки приложения, но не
    модуль — у них нет ни таблиц, ни экранов, ни тумблера.
    """
    global _index, _registered
    if _registered:
        return
    _index = load_index(root)
    _registered = True
    if not _index:
        return

    from app.agent.registry import ToolContext, ToolResult, tool
    from app.core.access import is_module_enabled

    listing = "\n".join(f"- {s.name}: {s.description}" for s in _index.values())

    @tool(
        name="load_skill",
        module="memory",   # знания всегда включены — инструмент доступен всем
        title="Открыть инструкцию",
        description=f"""
        Подгрузить подробную инструкцию для особой ситуации — и дальше
        действовать по ней. Зови ПЕРЕД тем, как действовать, когда разговор
        попадает в одну из этих ситуаций; в остальных случаях не зови.
        Доступные инструкции:
        {listing}
        """,
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(_index.keys()),
                         "description": "Имя инструкции из списка"},
            },
            "required": ["name"],
        },
        read_only=True,
        available=lambda: bool(_index),
    )
    def load_skill(ctx: ToolContext, name: str) -> ToolResult:
        skill = get(name)
        if skill is None:
            return ToolResult(summary=f"Инструкции «{name}» нет.", ok=False)
        if skill.module and not is_module_enabled(ctx.db, ctx.subject.id, skill.module):
            return ToolResult(summary="Эта инструкция относится к выключенному модулю — "
                                      "действуй без неё.", ok=False)
        text = body(name)
        if not text:
            return ToolResult(summary=f"Инструкция «{name}» не прочиталась.", ok=False)
        return ToolResult(
            summary=f"Инструкция «{skill.name}» (дальше действуй по ней):\n\n{text}",
        )
