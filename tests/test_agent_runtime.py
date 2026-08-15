"""The agent loop: tool calls, confirmations, traces, logging."""
import pytest

from app.agent import policy
from app.agent.llm import LLMResponse, LLMUnavailable, ToolCall
from app.agent.runtime import Agent, OFFLINE_REPLY, approve_action, reject_action
from app.core.models import ActionLog, ChatMessage, PendingAction
from app.modules.memory.models import BoardEntry
from tests.conftest import FakeLLM


def _call(name, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)])


def test_auto_mode_runs_the_tool_and_traces_it(db, member):
    policy.set_autonomy(db, member.family_id, 3)

    llm = FakeLLM([
        _call("remember", text="Соня не ест грибы"),
        LLMResponse(content="Запомнил."),
    ])
    reply = Agent(llm).respond(db, member, "запомни: Соня не ест грибы")

    assert reply.text == "Запомнил."
    assert [t.status for t in reply.traces] == ["done"]
    assert reply.traces[0].tool == "remember"
    assert reply.cards[0]["type"] == "board"

    assert db.query(ActionLog).filter(ActionLog.tool == "remember").count() == 1
    assert db.query(PendingAction).count() == 0


def test_ask_mode_prepares_the_action_instead_of_doing_it(db, member):
    policy.set_autonomy(db, member.family_id, 0)      # всё спрашивает

    llm = FakeLLM([
        _call("remember", text="купить хлеб"),
        LLMResponse(content="Подготовил, подтвердите."),
    ])
    reply = Agent(llm).respond(db, member, "запомни купить хлеб")

    assert [t.status for t in reply.traces] == ["awaiting"]
    pending = db.query(PendingAction).one()
    assert pending.status == "pending"
    assert pending.tool == "remember"
    # ничего не выполнено, пока человек не сказал «да»
    assert db.query(ActionLog).count() == 0

    card = reply.cards[0]
    assert card["type"] == "confirm" and card["pending_id"] == pending.id


def test_approving_runs_the_action_and_marks_it_confirmed(db, member):
    policy.set_autonomy(db, member.family_id, 0)
    Agent(FakeLLM([_call("remember", text="купить хлеб"),
                   LLMResponse(content="Подготовил.")])).respond(db, member, "запомни")

    pending = db.query(PendingAction).one()
    result = approve_action(db, pending.id, member)

    assert result.ok
    db.refresh(pending)
    assert pending.status == "approved"
    assert db.query(ActionLog).filter(ActionLog.mode == "confirmed").count() == 1


def test_rejecting_leaves_no_trace_of_the_action(db, member):
    policy.set_autonomy(db, member.family_id, 0)
    Agent(FakeLLM([_call("remember", text="что-то"),
                   LLMResponse(content="Подготовил.")])).respond(db, member, "запомни")

    pending = db.query(PendingAction).one()
    result = reject_action(db, pending.id, member)

    assert result.ok
    db.refresh(pending)
    assert pending.status == "rejected"
    assert db.query(ActionLog).count() == 0


def test_another_member_cannot_confirm_your_action(db, member, other):
    policy.set_autonomy(db, other.family_id, 0)
    Agent(FakeLLM([_call("remember", text="личное"),
                   LLMResponse(content="Подготовил.")])).respond(db, other, "запомни")

    pending = db.query(PendingAction).one()
    other = type(other)(family_id=other.family_id, username="sonya",
                         display_name="Соня", role="other")
    db.add(other)
    db.commit()

    result = approve_action(db, pending.id, other)
    assert not result.ok
    db.refresh(pending)
    assert pending.status == "pending"


def test_unknown_tool_does_not_break_the_conversation(db, member):
    llm = FakeLLM([_call("order_a_taxi", where="домой"), LLMResponse(content="Так я не умею.")])
    reply = Agent(llm).respond(db, member, "вызови такси")

    assert reply.text == "Так я не умею."
    assert reply.traces[0].status == "failed"


def test_offline_model_says_so_instead_of_crashing(db, member):
    class Broken(FakeLLM):
        def chat(self, *args, **kwargs):
            raise LLMUnavailable("нет связи")

    reply = Agent(Broken([])).respond(db, member, "привет")

    assert reply.text == OFFLINE_REPLY
    saved = db.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
    assert saved.content == OFFLINE_REPLY


def test_conversation_is_persisted_for_both_channels(db, member):
    policy.set_autonomy(db, member.family_id, 3)
    Agent(FakeLLM([LLMResponse(content="Здравствуйте.")])).respond(
        db, member, "привет", channel="telegram")

    roles = [m.role for m in db.query(ChatMessage).order_by(ChatMessage.id)]
    assert roles == ["user", "assistant"]
    assert all(m.channel == "telegram" for m in db.query(ChatMessage))


def test_only_allowed_tools_are_offered_to_the_model(db, other):
    from app.core.access import set_module_enabled

    set_module_enabled(db, other.id, "security", False)
    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, other, "что дома?")

    offered = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "get_security_log" not in offered
    assert "log_meal" in offered


# --- след инструментов в истории ------------------------------------------

def test_the_next_turn_knows_what_the_tools_returned(db, member):
    """Иначе ассистент не помнит ни номера записи, которую сам завёл, ни отказов.

    В истории едет только текст реплик, а `entry_id` живёт в ответе инструмента
    и до следующего хода не доживает. Ровно на этом ломалась поправка «пицца
    была 20 см»: подходящий инструмент нельзя было позвать без номера.
    """
    policy.set_autonomy(db, member.family_id, 3)
    Agent(FakeLLM([_call("remember", text="Соня не ест грибы"),
                   LLMResponse(content="Запомнил.")])).respond(db, member, "запомни про Соню")

    entry = db.query(BoardEntry).one()
    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "а что ты там записал?")

    said = [m["content"] for m in llm.calls[0]["messages"] if m["role"] == "assistant"][0]
    assert "Запомнил." in said                          # сама реплика на месте
    assert "remember(" in said                          # и чем она была добыта
    assert f"entry_id={entry.id}" in said               # номер записи доехал


def test_the_trail_never_reaches_the_human(db, member):
    """Приписка — служебная: она для модели, а панель читает ту же строку."""
    policy.set_autonomy(db, member.family_id, 3)
    Agent(FakeLLM([_call("remember", text="Соня не ест грибы"),
                   LLMResponse(content="Запомнил.")])).respond(db, member, "запомни про Соню")

    saved = db.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
    assert saved.content == "Запомнил."


def test_a_refusal_is_remembered_too(db, member):
    """«Поиск не настроен» на прошлом ходу — повод не долбиться в него снова."""
    policy.set_autonomy(db, member.family_id, 3)
    asked = LLMResponse(tool_calls=[ToolCall(id="call_lookup", name="lookup_product",
                                             arguments={"name": "пицца Пепперони Додо 20 см"})])
    Agent(FakeLLM([asked, LLMResponse(content="Не нашёл состав.")])).respond(
        db, member, "а сколько в ней?")

    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "ну ладно")

    said = [m["content"] for m in llm.calls[0]["messages"] if m["role"] == "assistant"][0]
    assert "lookup_product(" in said
    assert "failed" in said
