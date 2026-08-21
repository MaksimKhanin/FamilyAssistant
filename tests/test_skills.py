"""Скиллы: индекс, инструмент load_skill и фильтр по модулям (тикет #81)."""
from app.agent import policy, skills
from app.agent.runtime import run_tool_directly


def test_the_index_knows_the_recipe_book_skill():
    found = skills.load_index()

    assert "recipe-book" in found
    skill = found["recipe-book"]
    assert skill.module == "nutrition"
    assert "книгу рецептов" in skill.description


def test_the_body_comes_without_frontmatter():
    text = skills.body("recipe-book")

    assert text.startswith("Про книгу рецептов:")
    assert "---" not in text.split("\n")[0]
    assert "remember_recipe" in text


def test_load_skill_is_offered_to_the_model(db, member):
    names = {s.name for s in policy.available_tools(db, member)}
    assert "load_skill" in names


def test_load_skill_returns_the_instruction(db, member):
    result = run_tool_directly(db, member, "load_skill", {"name": "recipe-book"})

    assert result.ok
    assert "recipe-book" in result.summary
    assert "remember_recipe" in result.summary


def test_a_skill_of_a_disabled_module_refuses(db, member):
    from app.core.access import set_module_enabled

    set_module_enabled(db, member.id, "nutrition", False)
    result = run_tool_directly(db, member, "load_skill", {"name": "recipe-book"})

    assert not result.ok
    assert "выключенному модулю" in result.summary


def test_an_unknown_skill_refuses(db, member):
    result = run_tool_directly(db, member, "load_skill", {"name": "no-such"})

    assert not result.ok


def test_the_prompt_keeps_only_the_pointer(member):
    from app.agent.prompts import system_prompt

    prompt = system_prompt(member, ["nutrition", "memory"])

    assert "load_skill" in prompt
    # Тело инструкции в постоянный промпт больше не едет.
    assert "передавай в remember_recipe полем recipe" not in prompt
