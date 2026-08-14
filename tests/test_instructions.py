"""Характер ассистента и памятки по областям.

Две вещи, которые человек пишет о себе свободным текстом, и один вопрос к каждой:
доехало ли написанное до модели — и туда ли, куда человек рассчитывал. Памятка
про еду обязана быть в оценке тарелки и не обязана быть нигде больше: в этом вся
разница между «ассистент меня знает» и «ассистент возит мои болячки в каждый
запрос».
"""
import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMResponse
from app.agent.prompts import system_prompt
from app.agent.registry import ToolContext
from app.agent.runtime import Agent
from app.core import instructions
from app.core.access import set_module_enabled
from app.core.db import get_db
from app.core.models import ModuleMemo
from app.main import app
from app.modules.nutrition import tools as nutrition_tools
from app.modules.nutrition.vision import MealEstimate
from tests.conftest import FakeLLM

MEMO = "желчного нет, хронический гастрит, хочу набрать вес"
CHARACTER = "неформально и с иронией, без сюсюканья"


# --- хранение -------------------------------------------------------------

def test_character_is_personal_and_falls_back_to_the_default(db, head, member):
    instructions.set_character(db, head, CHARACTER)

    assert instructions.character(head) == CHARACTER
    # Чужой характер не приезжает, а свой у соседа — тот, что был всегда.
    assert instructions.character(member) == instructions.DEFAULT_CHARACTER

    instructions.set_character(db, head, "   ")
    assert instructions.character(head) == instructions.DEFAULT_CHARACTER
    assert instructions.own_character(head) == ""   # своего нет
    assert head.assistant_character is None         # пустую строку не храним


def test_memo_is_kept_per_person_and_per_area(db, head, member):
    instructions.set_memo(db, head.id, "nutrition", MEMO)
    instructions.set_memo(db, head.id, "security", "по средам уборщица")
    instructions.set_memo(db, member.id, "nutrition", "аллергия на орехи")

    assert instructions.memo(db, head.id, "nutrition") == MEMO
    assert instructions.memo(db, head.id, "security") == "по средам уборщица"
    assert instructions.memo(db, member.id, "nutrition") == "аллергия на орехи"
    assert instructions.memo(db, head.id, "memory") == ""


def test_emptying_a_memo_removes_the_row_rather_than_storing_a_blank(db, head):
    instructions.set_memo(db, head.id, "nutrition", MEMO)
    instructions.set_memo(db, head.id, "nutrition", "  ")

    assert instructions.memo(db, head.id, "nutrition") == ""
    assert db.query(ModuleMemo).count() == 0


def test_long_text_is_cut_instead_of_eating_the_context(db, head):
    instructions.set_character(db, head, "я" * 5000)
    instructions.set_memo(db, head.id, "nutrition", "я" * 5000)

    assert len(instructions.character(head)) == instructions.CHARACTER_LIMIT
    assert len(instructions.memo(db, head.id, "nutrition")) == instructions.MEMO_LIMIT


# --- системный промпт -----------------------------------------------------

def test_the_character_is_the_only_place_the_manner_is_written(db, head):
    prompt = system_prompt(head, ["nutrition"], character=CHARACTER)

    assert CHARACTER in prompt
    # Манеры в коде не осталось: спорить характеру не с чем.
    assert "тепло и спокойно" not in prompt
    # А то, что характер не меняет, на месте.
    assert "Не выдумывай данные" in prompt
    assert "нравоучений о еде" in prompt


def test_the_default_character_keeps_the_panel_talking_as_it_always_did(db, head):
    """Ничего не написал — ассистент говорит ровно так же, как до настройки."""
    prompt = system_prompt(head, ["nutrition"],
                           character=instructions.character(head))

    assert "тепло и спокойно, как внимательный член семьи" in prompt


def test_a_chat_of_a_person_who_wrote_nothing_carries_the_default(db, head):
    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, head, "привет")

    system = llm.calls[0]["messages"][0]["content"]
    assert instructions.DEFAULT_CHARACTER in system


def test_memo_travels_only_with_its_own_area(db, head):
    prompt = system_prompt(head, ["nutrition"],
                           memos=[("Питание", MEMO)])
    assert MEMO in prompt

    # Той же области не включили — памятке в промпте делать нечего.
    assert MEMO not in system_prompt(head, ["security"], memos=[])


def test_a_person_without_memos_gets_no_memo_block(db, head):
    assert "просил учитывать" not in system_prompt(head, ["nutrition"])


# --- разговор -------------------------------------------------------------

def test_the_chat_carries_character_and_memos_of_enabled_areas(db, head):
    instructions.set_character(db, head, CHARACTER)
    instructions.set_memo(db, head.id, "nutrition", MEMO)
    instructions.set_memo(db, head.id, "security", "по средам приходит уборщица")
    set_module_enabled(db, head.id, "security", False)

    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, head, "привет")

    system = llm.calls[0]["messages"][0]["content"]
    assert CHARACTER in system
    assert MEMO in system
    assert "уборщица" not in system          # модуль выключен — памятка осталась дома


def test_a_memo_of_one_person_never_reaches_another_persons_chat(db, head, member):
    instructions.set_memo(db, member.id, "nutrition", "аллергия на орехи")

    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, head, "привет")

    assert "орехи" not in llm.calls[0]["messages"][0]["content"]


# --- сценарий: еда --------------------------------------------------------

@pytest.fixture
def ctx(db, head):
    return ToolContext(db=db, actor=head, subject=head, channel="web", attachments={})


@pytest.fixture
def spy(monkeypatch):
    """Подменяет оценщик и запоминает контекст, который ему передали."""
    seen = {}

    def fake(text, context=None, facts=None, llm=None):
        seen["context"] = context
        return MealEstimate(title="Борщ", kcal=380, protein=14, fat=18, carbs=38)

    monkeypatch.setattr(nutrition_tools, "safe_estimate_from_text", fake)
    return seen


def test_the_meal_estimate_gets_the_nutrition_memo(db, head, ctx, spy):
    """Ради этого всё и заводилось: оценщик тарелки должен знать про гастрит."""
    instructions.set_memo(db, head.id, "nutrition", MEMO)

    nutrition_tools.log_meal(ctx, text="съел тарелку борща")

    assert MEMO in spy["context"]


def test_the_meal_estimate_of_a_person_without_a_memo_is_unchanged(db, head, ctx, spy):
    nutrition_tools.log_meal(ctx, text="съел тарелку борща")

    assert "просил учитывать" not in spy["context"]


def test_meal_ideas_are_asked_for_with_the_memo(db, head, ctx, monkeypatch):
    instructions.set_memo(db, head.id, "nutrition", MEMO)
    llm = FakeLLM([{"days": [{"title": "Завтра", "kcal": 2000,
                              "meals": [{"name": "овсянка", "slot": "завтрак", "kcal": 300}]}],
                    "comment": "Держитесь."}])
    monkeypatch.setattr(nutrition_tools, "llm_client", llm)

    result = nutrition_tools.suggest_meal_plan(ctx)

    assert result.ok
    assert MEMO in llm.calls[0]["user"]


# --- экран ----------------------------------------------------------------

@pytest.fixture
def as_head(db, head):
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.post("/login", data={"username": head.username, "password": "pw"},
                follow_redirects=False)
    yield client
    app.dependency_overrides.clear()


def test_the_screen_saves_the_character_and_the_memo(as_head, db, head):
    as_head.post("/settings/profile/character", data={"character": CHARACTER},
                 follow_redirects=False)
    as_head.post("/settings/profile/memo/nutrition", data={"memo": MEMO},
                 follow_redirects=False)

    db.refresh(head)
    assert instructions.character(head) == CHARACTER
    assert instructions.memo(db, head.id, "nutrition") == MEMO

    screen = as_head.get("/settings/profile")
    assert CHARACTER in screen.text
    assert MEMO in screen.text


def test_an_unknown_area_gets_no_memo(as_head, db, head):
    as_head.post("/settings/profile/memo/finance", data={"memo": "трачу много"},
                 follow_redirects=False)

    assert db.query(ModuleMemo).count() == 0
