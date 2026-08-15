"""Что поесть: одно блюдо в разговоре, рацион — на экране (ADR-0010).

Сценарий, ради которого всё это заведено, целиком человеческий: «у меня много
огурцов, придумай что-нибудь» — и в ответ одно блюдо с цифрами, а не расписание
на неделю. Понравилось — отметил, и блюдо осталось в плане питания; в следующем
разговоре ассистент о нём помнит.
"""
import pytest
from fastapi.testclient import TestClient

from app.agent import policy
from app.agent.registry import ToolContext
from app.core import instructions
from app.core.db import get_db
from app.main import app
from app.modules.nutrition import service, tools as nutrition_tools
from app.modules.nutrition.models import MealIdea
from tests.conftest import FakeLLM

MEMO = "желчного нет, хронический гастрит"
PREFERENCES = "не ем красное мясо и грибы, люблю рыбу"

DISH = {"title": "Салат из огурцов с фетой", "slot": "ужин", "kcal": 320, "protein": 12,
        "fat": 22, "carbs": 14, "portion": "тарелка ~280 г",
        "why": "Лёгкий ужин из того, что уже есть дома.", "question": ""}

RECIPE = {"title": "Салат из огурцов с фетой", "portions": 2, "kcal": 320, "protein": 12,
          "fat": 22, "carbs": 14,
          "ingredients": ["огурцы — 400 г", "фета — 100 г", "оливковое масло — 1 ст. л."],
          "steps": ["Нарезать огурцы.", "Раскрошить фету.", "Заправить маслом."],
          "note": ""}

PLAN = {"days": [
    {"title": "Завтра", "kcal": 2000, "meals": [
        {"name": "Овсянка с ягодами", "slot": "завтрак", "kcal": 380},
        {"name": "Суп и салат", "slot": "обед", "kcal": 620},
    ]},
    {"title": "День 2", "kcal": 1900, "meals": [
        {"name": "Творог с мёдом", "slot": "завтрак", "kcal": 340},
    ]},
], "comment": "Это идеи, а не предписание."}


@pytest.fixture
def ctx(db, member):
    return ToolContext(db=db, actor=member, subject=member, channel="web", attachments={})


@pytest.fixture
def as_member(db, member):
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.post("/login", data={"username": member.username, "password": "pw"},
                follow_redirects=False)
    yield client
    app.dependency_overrides.clear()


# --- разговор -------------------------------------------------------------

def test_a_question_about_food_gets_one_dish_not_a_week(db, member, ctx, monkeypatch):
    """Главное, ради чего всё переделано: в ответ одно блюдо, а не расписание."""
    llm = FakeLLM([DISH])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    result = nutrition_tools.suggest_dish(ctx, wish="что бы мне поесть?")

    assert result.ok
    assert result.card["type"] == "dish"
    assert result.card["title"] == DISH["title"]
    assert "320 ккал" in result.summary
    assert "days" not in result.data


def test_the_dish_is_built_from_the_products_the_person_named(db, member, ctx, monkeypatch):
    """«У меня много огурцов» — огурцы обязаны доехать до модели, а не потеряться."""
    llm = FakeLLM([DISH])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    nutrition_tools.suggest_dish(ctx, wish="у меня много огурцов, придумай что-нибудь",
                                 products="огурцы, фета")

    assert "огурцы" in llm.calls[0]["user"]


def test_the_dish_knows_everything_the_person_wrote_about_himself(db, member, ctx, monkeypatch):
    """Памятка, пожелания к рациону и отмеченное — складываются, а не спорят."""
    instructions.set_memo(db, member.id, "nutrition", MEMO)
    service.set_preferences(db, member.id, PREFERENCES)
    service.add_idea(db, member.id, "Запечённая треска", kcal=410, saved=True)

    llm = FakeLLM([DISH])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    nutrition_tools.suggest_dish(ctx, wish="что на ужин?")

    prompt = llm.calls[0]["user"]
    assert MEMO in prompt
    assert PREFERENCES in prompt
    assert "Запечённая треска" in prompt      # отмеченное блюдо можно вспомнить в разговоре
    assert "до нормы остаётся" in prompt      # остаток дня, а не норма целиком


def test_the_recipe_is_written_out_on_request(db, member, ctx, monkeypatch):
    llm = FakeLLM([RECIPE])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    result = nutrition_tools.dish_recipe(ctx, name="Салат из огурцов с фетой")

    assert result.ok
    assert result.card["type"] == "recipe"
    assert "огурцы — 400 г" in result.summary
    assert "1. Нарезать огурцы." in result.summary


def test_a_recipe_without_steps_is_not_a_recipe(db, member, ctx, monkeypatch):
    """Пустой ответ модели честнее выдать отказом, чем показать заголовок без шагов."""
    llm = FakeLLM([{"title": "Салат", "ingredients": ["огурцы"], "steps": []}])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    assert not nutrition_tools.dish_recipe(ctx, name="Салат").ok


def test_the_week_plan_is_not_offered_in_the_chat_at_all(db, member):
    """Инструмент рациона экранный: модель его не видит и предложить не может."""
    names = {spec.name for spec in policy.available_tools(db, member)}

    assert "suggest_dish" in names
    assert "dish_recipe" in names
    assert "suggest_meal_plan" not in names


# --- подбор на экране -----------------------------------------------------

def test_the_plan_survives_leaving_the_screen(db, member, ctx, monkeypatch):
    """Раньше подбор жил один переход, и отмечать в нём было нечего."""
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([PLAN]))

    nutrition_tools.suggest_meal_plan(ctx)
    days = service.plan_days(db, member.id)

    assert [day.title for day in days] == ["Завтра", "День 2"]
    assert [idea.title for idea in days[0].ideas] == ["Овсянка с ягодами", "Суп и салат"]
    assert days[0].kcal == 1000
    assert days[0].ideas[0].slot_label == "Завтрак"


def test_a_new_selection_keeps_what_the_person_marked(db, member, ctx, monkeypatch):
    """«Предложить другое» заменяет подбор, но не отмеченное: его для того и отмечали."""
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([PLAN, PLAN]))
    nutrition_tools.suggest_meal_plan(ctx)
    kept = service.plan_days(db, member.id)[0].ideas[0]
    service.toggle_saved(db, member.id, kept.id)

    nutrition_tools.suggest_meal_plan(ctx)

    assert [idea.title for idea in service.saved_ideas(db, member.id)] == ["Овсянка с ягодами"]
    # Отмеченное ушло из подбора в закреп, а неотмеченное прошлого раза исчезло.
    assert service.get_idea(db, member.id, kept.id).day_title is None
    assert db.query(MealIdea).filter(MealIdea.user_id == member.id).count() == 4


def test_unmarking_a_dish_from_the_chat_removes_it(db, member):
    """Блюдо из разговора держится только отметкой: снял — держать нечем."""
    from_chat = service.add_idea(db, member.id, "Салат из огурцов", kcal=320, saved=True)
    service.toggle_saved(db, member.id, from_chat.id)

    assert service.saved_ideas(db, member.id) == []
    assert service.get_idea(db, member.id, from_chat.id) is None


def test_ideas_belong_to_their_owner(db, member, other):
    idea = service.add_idea(db, member.id, "Салат из огурцов", saved=True)

    assert service.get_idea(db, other.id, idea.id) is None
    assert service.toggle_saved(db, other.id, idea.id) is None
    assert service.set_recipe(db, other.id, idea.id, "не твой рецепт") is None


# --- экран ----------------------------------------------------------------

def test_the_screen_keeps_the_wishes_about_the_diet(as_member, db, member):
    as_member.post("/nutrition/plan/preferences", data={"preferences": PREFERENCES},
                   follow_redirects=False)

    assert service.get_profile(db, member.id).preferences == PREFERENCES
    assert PREFERENCES in as_member.get("/nutrition/plan").text


def test_an_emptied_wish_is_erased_rather_than_stored_blank(as_member, db, member):
    service.set_preferences(db, member.id, PREFERENCES)

    as_member.post("/nutrition/plan/preferences", data={"preferences": "   "},
                   follow_redirects=False)

    assert service.get_profile(db, member.id).preferences is None


def test_the_screen_shows_the_selection_and_marks_a_dish(as_member, db, member, monkeypatch):
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([PLAN]))

    markup = as_member.post("/nutrition/plan").text
    assert "Овсянка с ягодами" in markup

    dish = service.plan_days(db, member.id)[0].ideas[0]
    as_member.post(f"/nutrition/plan/dish/{dish.id}/save", follow_redirects=False)

    assert service.get_idea(db, member.id, dish.id).saved


def test_the_recipe_is_written_next_to_the_dish(as_member, db, member, monkeypatch):
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([PLAN, RECIPE]))
    as_member.post("/nutrition/plan")
    dish = service.plan_days(db, member.id)[0].ideas[0]

    as_member.post(f"/nutrition/plan/dish/{dish.id}/recipe", follow_redirects=False)

    assert "Нарезать огурцы." in service.get_idea(db, member.id, dish.id).recipe
    assert "Нарезать огурцы." in as_member.get("/nutrition/plan").text


def test_a_dish_from_the_chat_lands_in_the_plan(as_member, db, member):
    """Кнопка на карточке в разговоре: блюдо уезжает в план, ответ приходит в ленту."""
    response = as_member.post("/nutrition/plan/dishes",
                              data={"title": DISH["title"], "slot": "ужин", "kcal": 320},
                              headers={"HX-Request": "true"})

    assert DISH["title"] in response.text
    saved = service.saved_ideas(db, member.id)
    assert [idea.title for idea in saved] == [DISH["title"]]
    assert saved[0].kcal == 320


def test_the_same_dish_is_not_kept_twice(as_member, db, member):
    """Кнопка остаётся в ленте навсегда — второе нажатие не должно двоить закреп."""
    for _ in range(2):
        as_member.post("/nutrition/plan/dishes",
                       data={"title": DISH["title"], "slot": "ужин", "kcal": 320},
                       headers={"HX-Request": "true"})

    assert len(service.saved_ideas(db, member.id)) == 1


def test_another_member_cannot_touch_the_plan(db, member, other):
    """Чужой план — не свой: цифры питания видит только их владелец."""
    idea = service.add_idea(db, member.id, "Салат из огурцов", saved=True)

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    other.password_hash = member.password_hash
    db.commit()
    client.post("/login", data={"username": other.username, "password": "pw"},
                follow_redirects=False)
    client.post(f"/nutrition/plan/dish/{idea.id}/save", follow_redirects=False)
    app.dependency_overrides.clear()

    assert service.get_idea(db, member.id, idea.id).saved
