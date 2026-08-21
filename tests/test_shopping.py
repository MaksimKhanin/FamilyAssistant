"""Модуль «Покупки»: общий список семьи (тикет #84)."""
from datetime import datetime, timedelta

from app.agent.runtime import run_tool_directly
from app.modules.shopping import service
from app.modules.shopping.models import ShoppingItem


def test_the_list_is_shared_across_the_family(db, member, other):
    run_tool_directly(db, member, "add_to_shopping_list", {"items": ["молоко", "хлеб"]})

    result = run_tool_directly(db, other, "show_shopping_list", {})

    assert "молоко" in result.summary and "хлеб" in result.summary


def test_duplicates_are_not_added_twice(db, member):
    run_tool_directly(db, member, "add_to_shopping_list", {"items": ["Молоко"]})
    result = run_tool_directly(db, member, "add_to_shopping_list", {"items": ["молоко"]})

    assert "уже" in result.summary.lower()
    assert db.query(ShoppingItem).count() == 1


def test_check_off_by_name(db, member):
    run_tool_directly(db, member, "add_to_shopping_list", {"items": ["молоко", "хлеб"]})

    result = run_tool_directly(db, member, "check_off_item", {"name": "молоко"})

    assert result.ok
    row = db.query(ShoppingItem).filter(ShoppingItem.text == "молоко").one()
    assert row.checked


def test_an_ambiguous_name_asks_instead_of_guessing(db, member):
    run_tool_directly(db, member, "add_to_shopping_list",
                      {"items": ["сыр твёрдый", "сырки глазированные"]})

    result = run_tool_directly(db, member, "check_off_item", {"name": "сыр"})

    assert not result.ok
    assert "переспроси" in result.summary


def test_checked_off_reappears_after_repurchase(db, member):
    run_tool_directly(db, member, "add_to_shopping_list", {"items": ["молоко"]})
    run_tool_directly(db, member, "check_off_item", {"name": "молоко"})

    result = run_tool_directly(db, member, "add_to_shopping_list", {"items": ["молоко"]})

    assert "Положил" in result.summary
    assert db.query(ShoppingItem).count() == 2


def test_old_checked_items_are_purged(db, member):
    added, _ = service.add_items(db, member.family_id, member.id, ["молоко"])
    service.check_off(db, member.family_id, "молоко")
    row = db.query(ShoppingItem).one()
    row.checked_at = datetime.utcnow() - timedelta(days=10)
    db.commit()

    assert service.purge_checked(db) == 1
    assert db.query(ShoppingItem).count() == 0


def test_the_screen_opens_and_shows_the_list(db, member):
    from starlette.testclient import TestClient

    from app.main import app

    service.add_items(db, member.family_id, member.id, ["гречка"])
    client = TestClient(app)
    client.post("/login", data={"username": "marina", "password": "pw"})

    page = client.get("/shopping")

    assert page.status_code == 200
    assert "гречка" in page.text


def test_tools_hide_when_the_module_is_off(db, member):
    from app.agent import policy
    from app.core.access import set_module_enabled

    set_module_enabled(db, member.id, "shopping", False)
    names = {s.name for s in policy.available_tools(db, member)}

    assert not {"add_to_shopping_list", "show_shopping_list", "check_off_item"} & names
