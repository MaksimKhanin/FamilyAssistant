"""Живые подсказки в чате и время суток в промпте (тикет #74)."""
from datetime import datetime

from app.agent.prompts import daypart, system_prompt
from app.core.models import ActionLog
from app.web.routes_chat import SUGGESTIONS_LIMIT, _suggestions


def test_suggestions_respect_disabled_modules(db, member):
    from app.core.access import set_module_enabled

    set_module_enabled(db, member.id, "security", False)

    picked = _suggestions(db, member)

    assert picked, "подсказки не должны пропасть вовсе"
    assert "Что было ночью дома?" not in picked
    assert len(picked) <= SUGGESTIONS_LIMIT


def test_a_frequent_tool_becomes_a_suggestion(db, member):
    for _ in range(5):
        db.add(ActionLog(user_id=member.id, tool="log_activity", summary="шаги"))
    db.commit()

    picked = _suggestions(db, member)

    assert "Прошёл 8000 шагов" in picked


def test_suggestions_fall_back_to_the_static_set(db, member):
    """Ни записей, ни привычек — человек видит знакомый набор, а не пустоту."""
    picked = _suggestions(db, member)

    assert picked
    assert all(isinstance(text, str) and text for text in picked)


def test_the_prompt_names_the_part_of_day(member):
    prompt = system_prompt(member, ["memory"], now=datetime(2026, 8, 9, 8, 30))

    assert "утро" in prompt
    assert daypart(23) == "ночь" and daypart(14) == "день"
