"""Книга рецептов: что человек запомнил, ассистент считает его вкусом (ADR-0012).

Путь, ради которого всё заведено, целиком человеческий: ассистент расписал рецепт,
предложил его запомнить — и в следующем разговоре предлагает еду, зная, что человек
уже готовит. Отдельной таблицы у книги нет: книга — это отмеченные блюда, а
известный рецепт — то же блюдо с расписанным рецептом.
"""
import pytest
from fastapi.testclient import TestClient

from app.agent import policy, stub
from app.agent.registry import ToolContext
from app.core.db import get_db
from app.main import app
from app.modules.nutrition import service, tools as nutrition_tools
from tests.conftest import FakeLLM


class SilentLLM:
    """Модель, которая не отвечает: расписать рецепт нечем, а блюдо запомнить надо."""

    configured = True

    def json_completion(self, system, user_content, **kwargs):
        from app.agent.llm import LLMUnavailable

        raise LLMUnavailable("модель не отвечает")


DISH = {"title": "Салат из огурцов с фетой", "slot": "ужин", "kcal": 320, "protein": 12,
        "fat": 22, "carbs": 14, "portion": "тарелка ~280 г",
        "why": "Лёгкий ужин из того, что уже есть дома.", "question": ""}

RECIPE = {"title": "Салат из огурцов с фетой", "portions": 2, "kcal": 320, "protein": 12,
          "fat": 22, "carbs": 14,
          "ingredients": ["огурцы — 400 г", "фета — 100 г", "оливковое масло — 1 ст. л."],
          "steps": ["Нарезать огурцы.", "Раскрошить фету.", "Заправить маслом."],
          "note": ""}

PLAN = {"days": [
    {"title": "Завтра", "kcal": 1000, "meals": [
        {"name": "Овсянка с ягодами", "slot": "завтрак", "kcal": 380},
        {"name": "Суп и салат", "slot": "обед", "kcal": 620},
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


# --- словами --------------------------------------------------------------

def test_remembering_a_dish_puts_it_in_the_book(db, member, ctx):
    """«Отметь блюдо» — запись в книге, и рецепта для этого не требуется."""
    result = nutrition_tools.remember_recipe(ctx, name="Запечённая треска", slot="ужин", kcal=410)

    assert result.ok
    book = service.saved_ideas(db, member.id)
    assert [idea.title for idea in book] == ["Запечённая треска"]
    assert book[0].kcal == 410 and book[0].slot_label == "Ужин"
    assert book[0].recipe is None
    assert result.card["type"] == "recipe-book"


def test_the_recipe_that_sounded_in_the_talk_is_stored_word_for_word(db, member, ctx):
    """В книгу ложится тот текст, который человек прочитал, а не написанный заново."""
    text = nutrition_tools.recipe_text(nutrition_tools._recipe_card(RECIPE, "Салат"))

    nutrition_tools.remember_recipe(ctx, name=RECIPE["title"], recipe=text)

    assert service.saved_ideas(db, member.id)[0].recipe == text


def test_a_recipe_nobody_wrote_yet_is_written_out_now(db, member, ctx, monkeypatch):
    """«Запомни рецепт борща», а рецепта ещё нет: инструмент расписывает его сам."""
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([RECIPE]))

    result = nutrition_tools.remember_recipe(ctx, name="Салат из огурцов", with_recipe=True)

    assert result.ok
    idea = service.saved_ideas(db, member.id)[0]
    assert idea.title == RECIPE["title"]          # название модель вправе уточнить
    assert "Нарезать огурцы." in idea.recipe


def test_the_dish_is_remembered_even_when_the_recipe_fails(db, member, ctx, monkeypatch):
    """Модель молчит — блюдо всё равно запомнено, и об этой половине сказано вслух."""
    monkeypatch.setattr(nutrition_tools, "llm_client", SilentLLM())

    result = nutrition_tools.remember_recipe(ctx, name="Салат из огурцов", with_recipe=True)

    assert result.ok
    assert [idea.title for idea in service.saved_ideas(db, member.id)] == ["Салат из огурцов"]
    assert "не вышло" in result.summary


def test_remembering_twice_keeps_one_record(db, member, ctx):
    """Кнопка живёт в ленте навсегда, и просьбу повторяют словами — запись одна."""
    nutrition_tools.remember_recipe(ctx, name="Салат из огурцов", kcal=320)
    nutrition_tools.remember_recipe(ctx, name="салат из огурцов", recipe="Нарезать и заправить.")

    book = service.saved_ideas(db, member.id)
    assert len(book) == 1
    assert book[0].kcal == 320                    # уже известное не затирается
    assert book[0].recipe == "Нарезать и заправить."


def test_a_dish_from_the_selection_is_marked_not_duplicated(db, member, ctx, monkeypatch):
    """«Запомни овсянку», когда она стоит в подборе, — это отметка, а не двойник."""
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([PLAN]))
    nutrition_tools.suggest_meal_plan(ctx)

    nutrition_tools.remember_recipe(ctx, name="Овсянка с ягодами")

    assert [idea.title for idea in service.saved_ideas(db, member.id)] == ["Овсянка с ягодами"]
    assert service.plan_days(db, member.id)[0].ideas[0].saved


def test_the_book_belongs_to_its_owner(db, member, other):
    idea = service.remember_dish(db, member.id, "Запечённая треска")

    assert service.forget_dish(db, other.id, idea.id) is False
    assert service.saved_ideas(db, other.id) == []


# --- рецепт в разговоре ---------------------------------------------------

def test_the_recipe_of_a_remembered_dish_lands_in_the_book_by_itself(db, member, ctx, monkeypatch):
    """Рецепт отмеченного блюда — его недостающая половина: запоминать отдельно нечего."""
    service.remember_dish(db, member.id, RECIPE["title"], kcal=320)
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([RECIPE]))

    result = nutrition_tools.dish_recipe(ctx, name=RECIPE["title"])

    assert result.card["remembered"]
    assert "Нарезать огурцы." in service.saved_ideas(db, member.id)[0].recipe


def test_a_recipe_of_an_unknown_dish_is_not_stored_silently(db, member, ctx, monkeypatch):
    """Рецепт в разговоре нигде не лежит, пока его не запомнили, — и ассистенту сказано предложить."""
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([RECIPE]))

    result = nutrition_tools.dish_recipe(ctx, name=RECIPE["title"])

    assert not result.card["remembered"]
    assert service.saved_ideas(db, member.id) == []
    assert "remember_recipe" in result.summary


# --- книга едет в подбор --------------------------------------------------

def test_the_book_is_what_the_assistant_leans_on(db, member, ctx, monkeypatch):
    """Ради этого книга и заведена: известные рецепты — вкус человека."""
    service.remember_dish(db, member.id, "Запечённая треска", kcal=410,
                          recipe="Треска, овощи, 20 минут в духовке.")
    llm = FakeLLM([DISH])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    nutrition_tools.suggest_dish(ctx, wish="что на ужин?")

    prompt = llm.calls[0]["user"]
    assert "Книга рецептов" in prompt
    assert "Запечённая треска (≈410 ккал, рецепт записан)" in prompt


def test_the_tool_is_offered_to_the_model(db, member):
    assert "remember_recipe" in {spec.name for spec in policy.available_tools(db, member)}


def test_the_offline_mode_tells_the_phrases_apart():
    """Офлайн-разбор: «запомни рецепт» — не факт о человеке и не просьба расписать."""
    available = {"remember", "remember_recipe", "dish_recipe"}

    assert stub._pick_tool("запомни рецепт борща", available) == "remember_recipe"
    assert stub._pick_tool("отметь блюдо", available) == "remember_recipe"
    assert stub._pick_tool("запомни, что я не ем грибы", available) == "remember"
    assert stub._arguments_for("remember_recipe", "запомни рецепт борща") == {
        "name": "борща", "with_recipe": True}


# --- экран ----------------------------------------------------------------

def test_the_screen_shows_the_book_and_its_recipes(as_member, db, member):
    service.remember_dish(db, member.id, "Запечённая треска", kcal=410,
                          recipe="Треска, овощи, 20 минут в духовке.")

    markup = as_member.get("/nutrition/recipes").text

    assert "Запечённая треска" in markup
    assert "20 минут в духовке" in markup


def test_the_button_in_the_talk_remembers_the_recipe(as_member, db, member):
    """Кнопка на карточке рецепта: в книгу уезжает и блюдо, и его текст."""
    response = as_member.post("/nutrition/recipes",
                              data={"title": RECIPE["title"], "kcal": 320,
                                    "recipe": "Нарезать огурцы. Заправить маслом."},
                              headers={"HX-Request": "true"})

    assert RECIPE["title"] in response.text
    book = service.saved_ideas(db, member.id)
    assert [idea.title for idea in book] == [RECIPE["title"]]
    assert "Заправить маслом." in book[0].recipe


def test_a_dish_from_the_talk_is_remembered_without_a_recipe(as_member, db, member):
    """Та же ручка для карточки блюда: рецепта у него ещё нет, и это нормально."""
    response = as_member.post("/nutrition/recipes",
                              data={"title": DISH["title"], "slot": "ужин", "kcal": 320},
                              headers={"HX-Request": "true"})

    assert DISH["title"] in response.text
    book = service.saved_ideas(db, member.id)
    assert [idea.title for idea in book] == [DISH["title"]]
    assert book[0].kcal == 320 and book[0].recipe is None


def test_the_same_dish_is_not_remembered_twice(as_member, db, member):
    for _ in range(2):
        as_member.post("/nutrition/recipes",
                       data={"title": DISH["title"], "slot": "ужин", "kcal": 320},
                       headers={"HX-Request": "true"})

    assert len(service.saved_ideas(db, member.id)) == 1


def test_a_recipe_is_written_out_for_a_remembered_dish(as_member, db, member, monkeypatch):
    monkeypatch.setattr(nutrition_tools, "llm_client", FakeLLM([RECIPE]))
    idea = service.remember_dish(db, member.id, RECIPE["title"], kcal=320)

    as_member.post(f"/nutrition/recipes/{idea.id}/recipe", follow_redirects=False)

    assert "Нарезать огурцы." in service.get_idea(db, member.id, idea.id).recipe
    assert "Нарезать огурцы." in as_member.get("/nutrition/recipes").text


def test_a_dish_can_be_removed_from_the_book(as_member, db, member):
    idea = service.remember_dish(db, member.id, "Запечённая треска")

    as_member.post(f"/nutrition/recipes/{idea.id}/forget", follow_redirects=False)

    assert service.saved_ideas(db, member.id) == []


def test_another_member_cannot_touch_the_book(db, member, other):
    """Чужая книга — не своя: вкусы питания принадлежат их владельцу."""
    idea = service.remember_dish(db, member.id, "Запечённая треска")

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    other.password_hash = member.password_hash
    db.commit()
    client.post("/login", data={"username": other.username, "password": "pw"},
                follow_redirects=False)
    markup = client.get("/nutrition/recipes").text
    client.post(f"/nutrition/recipes/{idea.id}/forget", follow_redirects=False)
    app.dependency_overrides.clear()

    assert "Запечённая треска" not in markup
    assert service.get_idea(db, member.id, idea.id).saved
