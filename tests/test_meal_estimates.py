"""Оценка съеденного: цифры сразу, уточняющий вопрос — следом.

Разбор трейса показал ровно эту дыру: инструмент уходил в «ждёт подтверждения»,
оценка не считалась, и ассистент отвечал «записал» без единой цифры. Тесты держат
починенное поведение: записать черновик, назвать числа, спросить одно.
"""
import pytest

from app.agent.registry import ToolContext
from app.modules.memory import knowledge
from app.modules.nutrition import service, tools, vision
from app.modules.nutrition.models import STATUS_DRAFT
from app.modules.nutrition.vision import MealEstimate
from tests.conftest import FakeLLM


@pytest.fixture
def ctx(db, head):
    return ToolContext(db=db, actor=head, subject=head, channel="web", attachments={})


@pytest.fixture
def spy(monkeypatch):
    """Подменяет оценщик и запоминает, что ему передали."""
    seen = {}

    def fake(text, context=None, llm=None):
        seen["text"] = text
        seen["context"] = context
        return seen.get("estimate") or MealEstimate(
            title="Борщ со сметаной", kcal=380, protein=14, fat=18, carbs=38,
            portion="тарелка ~350 г", components=["борщ ~330 г", "сметана ~20 г"],
            question="Сколько примерно грамм было в тарелке?",
        )

    monkeypatch.setattr(tools, "safe_estimate_from_text", fake)
    return seen


def test_the_reply_carries_the_numbers_and_the_question(ctx, spy):
    result = tools.log_meal(ctx, text="съел тарелку борща со сметаной")

    assert result.ok
    assert "380 ккал" in result.summary
    assert "Б 14 / Ж 18 / У 38" in result.summary
    assert "тарелка ~350 г" in result.summary
    assert "борщ ~330 г" in result.summary                  # из чего сложилась цифра
    assert "Сколько примерно грамм" in result.summary       # вопрос человеку
    assert result.data["question"]


def test_the_draft_is_written_before_the_question_is_asked(ctx, db, head, spy):
    """Человек вправе не ответить — приём пищи из-за этого теряться не должен."""
    tools.log_meal(ctx, text="борщ")

    meal = db.query(service.Meal).filter(service.Meal.user_id == head.id).one()
    assert meal.status == STATUS_DRAFT
    assert meal.kcal == 380


def test_weight_and_cooking_reach_the_estimator(ctx, spy):
    tools.log_meal(ctx, text="съел картошку", weight_g=250, cooking="жареная на масле")

    assert "съел картошку" in spy["text"]
    assert "250 г" in spy["text"]
    assert "жареная на масле" in spy["text"]


def test_the_estimator_is_told_who_it_is_counting_for(ctx, db, head, spy):
    """Аллергия и цель меняют оценку сильнее, чем кажется, а модель о них не спросит."""
    section = knowledge.create_section(db, head.id, "Личное")
    board = knowledge.create_board(db, head.id, section.id, "Здоровье")
    knowledge.add_entry(db, head.id, board.id, "Максим не ест сахар")
    service.update_profile(db, head.id, daily_kcal=2400)

    tools.log_meal(ctx, text="чай с печеньем")

    assert "2400 ккал" in spy["context"]
    assert "не ест сахар" in spy["context"]


def test_a_measuring_board_does_not_crowd_out_what_matters(ctx, db, head, spy):
    """«02:50 170» об этом человеке не говорит ничего, а вытеснило бы аллергию:
    доски, которые ведут счёт, в выжимку не идут."""
    section = knowledge.create_section(db, head.id, "Личное")
    facts = knowledge.create_board(db, head.id, section.id, "Здоровье")
    knowledge.add_entry(db, head.id, facts.id, "Максим не ест сахар")
    log = knowledge.create_board(db, head.id, section.id, "Кормления")
    knowledge.add_event_type(db, head.id, log.id, "кормление", "мл")
    for hour in range(12):
        knowledge.add_entry(db, head.id, log.id, f"{hour}:50 170", llm=FakeLLM([{"events": []}]))

    tools.log_meal(ctx, text="чай с печеньем")

    assert "не ест сахар" in spy["context"]
    assert "170" not in spy["context"]


def test_no_question_when_there_is_nothing_to_ask(ctx, spy):
    spy["estimate"] = MealEstimate(title="Овсянка", kcal=320, protein=12, fat=14, carbs=34,
                                   portion="порция 250 г", question=None)

    result = tools.log_meal(ctx, text="съел 250 г овсянки")

    assert "320 ккал" in result.summary
    assert "Спроси" not in result.summary


def test_low_confidence_is_said_out_loud(ctx, spy):
    spy["estimate"] = MealEstimate(title="Лягушка", kcal=90, protein=16, fat=3, carbs=0,
                                   confidence="low", note="Блюдо редкое, взял ближайший аналог")

    result = tools.log_meal(ctx, text="я съел лягушку")

    assert "Уверенности мало" in result.summary
    assert "ближайший аналог" in result.summary


def test_an_empty_call_records_nothing(ctx, spy):
    result = tools.log_meal(ctx)

    assert not result.ok
    assert "Нечего записывать" in result.summary


# --- уточнение после вопроса ----------------------------------------------

def test_answering_the_weight_question_actually_moves_the_numbers(ctx, db, head, spy):
    """Иначе вопрос «сколько грамм?» — пустая вежливость: ответ ничего не меняет."""
    tools.log_meal(ctx, text="съел тарелку борща")
    meal_id = db.query(service.Meal).filter(service.Meal.user_id == head.id).one().id

    spy["estimate"] = MealEstimate(title="Борщ", kcal=520, protein=19, fat=25, carbs=52,
                                   portion="тарелка ~400 г")
    result = tools.confirm_meal(ctx, meal_id=meal_id, weight_g=400)

    assert "520 ккал" in result.summary
    assert "тарелка ~400 г" in result.summary
    assert "400 г" in spy["text"]                       # уточнение дошло до оценщика


def test_named_numbers_are_taken_as_they_are(ctx, db, head, spy):
    """Человек сказал цифру — она главнее любой оценки, пересчитывать нечего."""
    tools.log_meal(ctx, text="съел борщ")
    meal_id = db.query(service.Meal).filter(service.Meal.user_id == head.id).one().id
    spy["text"] = None

    result = tools.confirm_meal(ctx, meal_id=meal_id, kcal=450, weight_g=400)

    assert "450 ккал" in result.summary
    assert spy["text"] is None                          # оценщика не звали


# --- разбор ответа модели -------------------------------------------------

def test_components_and_question_survive_the_parsing():
    raw = {"title": "Борщ", "kcal": "380", "protein": 14, "fat": 18, "carbs": 38,
           "portion": "тарелка ~350 г",
           "components": ["борщ ~330 г", "сметана ~20 г", ""],
           "question": "  Сколько грамм?  ", "confidence": "medium"}

    estimate = vision._coerce(raw, fallback_title="?")

    assert estimate.kcal == 380
    assert estimate.components == ["борщ ~330 г", "сметана ~20 г"]     # пустое отброшено
    assert estimate.question == "Сколько грамм?"


def test_an_empty_question_becomes_nothing_to_ask():
    estimate = vision._coerce({"title": "Овсянка", "kcal": 320, "question": ""}, fallback_title="?")

    assert estimate.question is None
    assert estimate.components == []
