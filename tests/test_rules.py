"""Правила: как ассистент дописывает себе промпт по словам человека.

Проверяется здесь не «инструмент вернул ok», а то единственное, ради чего правило
заводится: сказанное однажды доезжает до модели в следующем разговоре — и доезжает
выше того, что не отменяется ничем.
"""
import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMResponse, ToolCall
from app.agent.prompts import TONE, system_prompt
from app.agent.registry import ToolContext
from app.agent.runtime import Agent
from app.core import instructions
from app.core.db import get_db
from app.main import app
from app.modules.memory import knowledge
from app.modules.memory.models import Board, BoardEntry, Section
from app.modules.memory.tools import (add_memo, drop_rule, remember, set_autonomy,
                                      set_board_instruction, set_character, set_rule,
                                      set_tool_mode)
from tests.conftest import FakeLLM


def ctx(db, user, subject=None) -> ToolContext:
    return ToolContext(db=db, actor=user, subject=subject or user)


def _call(name, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)])


# --- реестр: доска заводится тем, кто в неё пишет ---------------------------------

def test_reading_rules_does_not_create_the_board(db, member):
    """Чтение идёт на каждом ходу: заводить реестр тому, кто ни о чём не
    договаривался, значит выдать ему пустую доску за то, что он поговорил."""
    assert knowledge.rules_for_prompt(db, member.id) == []
    assert knowledge.list_rules(db, member.id) == []
    assert db.query(Board).count() == 0
    assert db.query(Section).count() == 0


def test_first_rule_creates_the_registry_board(db, member):
    result = set_rule(ctx(db, member), text="записывай моё состояние на доску «Самочувствие»")

    assert result.ok
    board = knowledge.rules_board(db, member.id)
    assert board is not None and board.name == knowledge.RULES_BOARD_NAME
    entry = db.query(BoardEntry).one()
    # Автор — ассистент: правило написал он, пусть и с чужих слов.
    assert entry.by_assistant and entry.author_id is None
    assert result.data["rule_id"] == entry.id


def test_replaces_takes_the_old_rule_off(db, member):
    """Две строки об одном и том же противоречат друг другу молча — и разбирать
    это пришлось бы модели посреди разговора."""
    first = set_rule(ctx(db, member), text="не предлагай сладкое на завтрак")
    set_rule(ctx(db, member), text="не предлагай сладкое вообще",
             replaces=first.data["rule_id"])

    texts = [text for _, text in knowledge.rules_for_prompt(db, member.id)]
    assert texts == ["не предлагай сладкое вообще"]


def test_a_rule_longer_than_the_limit_is_trimmed(db, member):
    set_rule(ctx(db, member), text="я" * (knowledge.RULE_LIMIT + 50))

    (_, text), = knowledge.rules_for_prompt(db, member.id)
    assert len(text) == knowledge.RULE_LIMIT


def test_the_registry_refuses_to_grow_past_the_limit(db, member):
    for number in range(knowledge.RULES_MAX):
        assert set_rule(ctx(db, member), text=f"правило {number}").ok

    result = set_rule(ctx(db, member), text="ещё одно")

    # Отказ, а не тихое вытеснение: правило, которое ассистент забыл сам, человек
    # считает действующим.
    assert not result.ok
    assert "правило 0" in result.summary
    assert len(knowledge.rules_for_prompt(db, member.id)) == knowledge.RULES_MAX


def test_replacing_works_even_at_the_limit(db, member):
    first = set_rule(ctx(db, member), text="правило 0")
    for number in range(1, knowledge.RULES_MAX):
        set_rule(ctx(db, member), text=f"правило {number}")

    result = set_rule(ctx(db, member), text="правило 0, но иначе",
                      replaces=first.data["rule_id"])

    assert result.ok
    assert len(knowledge.rules_for_prompt(db, member.id)) == knowledge.RULES_MAX


# --- снятие: только правила и только свои -----------------------------------------

def test_drop_rule_removes_it_from_the_prompt(db, member):
    rule = set_rule(ctx(db, member), text="записывай состояние на доску")

    assert drop_rule(ctx(db, member), rule_id=rule.data["rule_id"]).ok
    assert knowledge.rules_for_prompt(db, member.id) == []


def test_drop_rule_does_not_touch_the_assistants_memory(db, member):
    """«Память ассистента» — тоже его записи, но правилами они не являются:
    забыть грибы должен forget, а не отмена уговора."""
    remembered = remember(ctx(db, member), text="Соня не ест грибы")
    set_rule(ctx(db, member), text="какое-нибудь правило")

    assert not drop_rule(ctx(db, member), rule_id=remembered.data["entry_id"]).ok
    assert db.get(BoardEntry, remembered.data["entry_id"]) is not None


def test_a_strangers_rule_is_out_of_reach(db, member, other):
    theirs = set_rule(ctx(db, other), text="чужое правило")

    assert not drop_rule(ctx(db, member), rule_id=theirs.data["rule_id"]).ok
    assert len(knowledge.rules_for_prompt(db, other.id)) == 1


def test_rules_are_personal(db, member, other):
    set_rule(ctx(db, member), text="моё правило")

    assert [t for _, t in knowledge.rules_for_prompt(db, member.id)] == ["моё правило"]
    assert knowledge.rules_for_prompt(db, other.id) == []


# --- реестр не путается с обычными досками ----------------------------------------

def test_the_registry_stays_out_of_the_board_list(db, member):
    """Содержимое реестра и так едет в промпт целиком; строка в перечне досок
    звала бы модель писать туда write_entry — мимо set_rule и мимо человека."""
    set_rule(ctx(db, member), text="записывай состояние на доску")

    assert knowledge.RULES_BOARD_NAME not in knowledge.boards_prompt(db, member.id)


def test_the_registry_is_not_a_fact_about_the_person(db, member):
    set_rule(ctx(db, member), text="записывай состояние на доску")
    remember(ctx(db, member), text="Соня не ест грибы")

    facts = knowledge.person_facts(db, member.id)

    assert "Соня не ест грибы" in facts
    assert "записывай состояние на доску" not in facts


# --- промпт ------------------------------------------------------------------------

def test_rules_stand_above_what_nothing_overrides(db, member):
    """Порядок здесь смысловой: правило «считай точно» не должно читаться позже,
    чем «оценки называй оценками»."""
    prompt = system_prompt(member, ["memory"], rules=[(7, "записывай состояние на доску")])

    first_line = TONE.splitlines()[0]
    assert prompt.index("записывай состояние на доску") < prompt.index(first_line)
    assert "#7" in prompt


def test_a_rule_said_once_reaches_the_next_conversation(db, member):
    """Сквозная проверка всей затеи: человек сказал — ассистент сохранил — на
    следующем ходу это уже часть его промпта, без единого напоминания."""
    from app.agent import policy
    policy.set_autonomy(db, member.family_id, 3)

    Agent(FakeLLM([_call("set_rule", text="записывай моё состояние на доску «Самочувствие» "
                                          "со временем сообщения"),
                   LLMResponse(content="Договорились.")])
          ).respond(db, member, "с этого момента фиксируй моё состояние")

    # Ответ нарочно без «записал»: отчёт о работе без единого вызова инструмента
    # рантайм не пропускает и просит модель сделать по-настоящему, а здесь
    # проверяется не он, а промпт следующего хода.
    llm = FakeLLM([LLMResponse(content="Хорошо, услышал.")])
    Agent(llm).respond(db, member, "проснулся, чувствую себя усталым")

    system = llm.calls[-1]["messages"][0]["content"]
    assert "со временем сообщения" in system


# --- характер ----------------------------------------------------------------------

def test_set_character_rewrites_the_profile_field(db, member):
    result = set_character(ctx(db, member), text="сухо и по делу, без смайликов")

    assert result.ok
    assert instructions.own_character(member) == "сухо и по делу, без смайликов"
    # Модель должна знать, что она себе поставила, — иначе следующей поправкой
    # она это затрёт.
    assert "сухо и по делу" in result.summary


def test_set_character_refuses_to_empty_the_field(db, member):
    """Вернуть умолчание — осознанное движение человека на своём экране, а не
    побочный эффект неудачной формулировки в разговоре."""
    instructions.set_character(db, member, "сухо и по делу")

    assert not set_character(ctx(db, member), text="   ").ok
    assert instructions.own_character(member) == "сухо и по делу"


# --- спрашивать или делать (ADR-0012) ----------------------------------------------

def test_asking_to_be_asked_changes_the_dial_not_just_the_words(db, member):
    """«Спрашивай, прежде чем писать на доски» — это ручка, а не уговор.

    Правилом такое не делается: правило ассистент читает и старается соблюдать,
    а подтверждение ставит система, и без её ручки человек повторял бы просьбу
    каждый раз.
    """
    from app.agent import policy, registry

    policy.set_autonomy(db, member.family_id, 3)

    result = set_tool_mode(ctx(db, member), tool="write_entry", mode="ask")

    assert result.ok
    assert policy.resolve_mode(db, member, registry.get("write_entry")) == "ask"


def test_the_assistant_cannot_hand_itself_what_the_administrator_switched_off(db, member):
    from app.agent import policy

    policy.set_mode(db, member.family_id, "notify_family", "off")

    result = set_tool_mode(ctx(db, member), tool="notify_family", mode="auto")

    assert not result.ok
    # Отказ пересказывают человеку, поэтому в нём должно быть сказано и кто
    # выключил, и где это меняется.
    assert "администратор" in result.summary
    assert "Агент и инструменты" in result.summary


def test_an_unknown_tool_comes_back_with_the_list(db, member):
    result = set_tool_mode(ctx(db, member), tool="сделай_хорошо", mode="ask")

    assert not result.ok
    assert "write_entry" in result.summary


def test_the_autonomy_tool_touches_only_this_person(db, member, other):
    from app.agent import policy

    policy.set_autonomy(db, member.family_id, 1)

    assert set_autonomy(ctx(db, member), level=3).ok

    assert policy.dials(db, member).autonomy == 3
    assert policy.dials(db, other).autonomy == 1


def test_the_autonomy_tool_can_give_the_dial_back_to_the_house(db, member):
    from app.agent import policy

    policy.set_autonomy(db, member.family_id, 2)
    policy.set_own_autonomy(db, member, 0)

    result = set_autonomy(ctx(db, member), follow_family=True)

    assert result.ok
    assert policy.dials(db, member).follows_family
    assert policy.dials(db, member).autonomy == 2


def test_the_autonomy_tool_asks_again_instead_of_guessing_a_level(db, member):
    result = set_autonomy(ctx(db, member))

    assert not result.ok
    assert member.autonomy is None


def test_the_dials_reach_the_model_in_the_next_prompt(db, member):
    """Ассистент должен видеть, что у него уже стоит: иначе он крутит вслепую."""
    from app.agent import policy

    policy.set_autonomy(db, member.family_id, 1)
    policy.set_own_autonomy(db, member, 3)
    policy.set_own_mode(db, member, "write_entry", "ask")

    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "как дела")

    system = llm.calls[-1]["messages"][0]["content"]
    assert "Максимально самостоятельно" in system
    assert "он выбрал себе сам" in system
    assert "write_entry" in system and "спрашиваешь разрешения" in system


# --- памятка -----------------------------------------------------------------------

def test_add_memo_appends_instead_of_replacing(db, member):
    instructions.set_memo(db, member.id, "nutrition", "нет желчного")

    result = add_memo(ctx(db, member), area="Питание", text="и гастрит")

    assert result.ok
    memo = instructions.memo(db, member.id, "nutrition")
    assert "нет желчного" in memo and "и гастрит" in memo


def test_add_memo_refuses_an_unknown_area(db, member):
    result = add_memo(ctx(db, member), area="Финансы", text="что-нибудь")

    assert not result.ok
    assert "Питание" in result.summary


def test_a_full_memo_is_not_silently_trimmed(db, member):
    """Памятка — слова человека: молча укоротить их значит соврать ему о том,
    что он теперь просил учитывать."""
    instructions.set_memo(db, member.id, "nutrition", "я" * instructions.MEMO_LIMIT)

    result = add_memo(ctx(db, member), area="Питание", text="и ещё вот это")

    assert not result.ok
    assert "Профиль и агент" in result.summary
    assert len(instructions.memo(db, member.id, "nutrition")) == instructions.MEMO_LIMIT


# --- инструкция доски ---------------------------------------------------------------

def test_set_board_instruction_rewrites_it(db, member):
    section = knowledge.create_section(db, member.id, "Малыш")
    board = knowledge.create_board(db, member.id, section.id, "Кормления", "старая инструкция")

    result = set_board_instruction(ctx(db, member), board="Кормления",
                                   instruction="время и объём в миллилитрах")

    assert result.ok
    db.refresh(board)
    assert board.instruction == "время и объём в миллилитрах"
    assert board.section_id == section.id


def test_set_board_instruction_needs_ownership(db, member, other):
    section = knowledge.create_section(db, other.id, "Малыш")
    board = knowledge.create_board(db, other.id, section.id, "Кормления", "их инструкция")
    knowledge.share_board(db, other.id, board.id, member.id, right="edit")

    result = set_board_instruction(ctx(db, member), board="Кормления",
                                   instruction="по-моему")

    # Инструкция меняет поведение ассистента для всех допущенных — правит её владелец.
    assert not result.ok
    db.refresh(board)
    assert board.instruction == "их инструкция"


def test_set_board_instruction_asks_about_an_unknown_board(db, member):
    result = set_board_instruction(ctx(db, member), board="Счётчики", instruction="что-нибудь")

    assert not result.ok
    assert "Счётчики" in result.summary


# --- экран -------------------------------------------------------------------------

@pytest.fixture
def as_member(db, member):
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.post("/login", data={"username": member.username, "password": "pw"},
                follow_redirects=False)
    yield client
    app.dependency_overrides.clear()


def test_the_profile_screen_shows_the_rules(as_member, db, member):
    """Правило, которого человек не видит, — сюрприз, а не настройка."""
    set_rule(ctx(db, member), text="записывай моё состояние на доску «Самочувствие»")

    screen = as_member.get("/settings/profile").text

    assert "записывай моё состояние на доску «Самочувствие»" in screen
    # Ссылка на сам реестр: правило правится там, где лежит, а не вторым полем здесь.
    assert knowledge.rules_url(db, member.id).replace("&", "&amp;") in screen


def test_the_profile_screen_is_quiet_without_rules(as_member, db, member):
    screen = as_member.get("/settings/profile").text

    assert "Правила" not in screen
