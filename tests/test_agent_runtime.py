"""The agent loop: tool calls, confirmations, traces, logging."""
import pytest

from app.agent import policy
from app.agent.llm import LLMResponse, LLMUnavailable, ToolCall
from app.agent.runtime import (
    Agent, OFFLINE_REPLY, UNBACKED_REPLY, approve_action, reject_action,
)
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
    result = approve_action(db, pending.id, member,
                            llm=FakeLLM([LLMResponse(content="Готово, записала себе.")]))

    assert result.ok
    db.refresh(pending)
    assert pending.status == "approved"
    assert db.query(ActionLog).filter(ActionLog.mode == "confirmed").count() == 1


def test_approving_speaks_with_the_models_own_words(db, member):
    """Реплика после «да» — слова модели, характером ассистента, а не сырой
    технический `summary` инструмента (#78)."""
    policy.set_autonomy(db, member.family_id, 0)
    Agent(FakeLLM([_call("remember", text="купить хлеб"),
                   LLMResponse(content="Подготовил.")])).respond(db, member, "запомни")

    pending = db.query(PendingAction).one()
    result = approve_action(db, pending.id, member,
                            llm=FakeLLM([LLMResponse(content="Записала, не забуду.")]))

    assert result.summary == "Записала, не забуду."
    last = db.query(ChatMessage).order_by(ChatMessage.id.desc()).first()
    assert last.role == "assistant"
    assert last.content == "Записала, не забуду."


def test_approving_falls_back_to_the_raw_summary_when_the_model_is_offline(db, member):
    policy.set_autonomy(db, member.family_id, 0)
    Agent(FakeLLM([_call("remember", text="купить хлеб"),
                   LLMResponse(content="Подготовил.")])).respond(db, member, "запомни")

    pending = db.query(PendingAction).one()

    class Broken(FakeLLM):
        def chat(self, *args, **kwargs):
            raise LLMUnavailable("недоступна")

    result = approve_action(db, pending.id, member, llm=Broken([]))

    assert result.ok
    assert "Запомнил" in result.summary


def test_rejecting_leaves_no_trace_of_the_action(db, member):
    policy.set_autonomy(db, member.family_id, 0)
    Agent(FakeLLM([_call("remember", text="что-то"),
                   LLMResponse(content="Подготовил.")])).respond(db, member, "запомни")

    pending = db.query(PendingAction).one()
    result = reject_action(db, pending.id, member,
                           llm=FakeLLM([LLMResponse(content="Хорошо, не буду.")]))

    assert result.ok
    db.refresh(pending)
    assert pending.status == "rejected"
    assert db.query(ActionLog).count() == 0
    last = db.query(ChatMessage).order_by(ChatMessage.id.desc()).first()
    assert last.role == "assistant"
    assert last.content == "Хорошо, не буду."


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

    messages = llm.calls[0]["messages"]
    said = [m["content"] for m in messages if m["role"] == "assistant"][0]
    trail = [m["content"] for m in messages[1:] if m["role"] == "system"][0]
    assert said == "Запомнил."                          # сама реплика на месте
    assert "remember(" in trail                         # и чем она была добыта
    assert f"entry_id={entry.id}" in trail              # номер записи доехал


def test_the_trail_is_not_written_in_the_assistants_voice(db, member):
    """Иначе модель дочитывает узор «слова + перечень вызовов» как свою роль.

    Прогон #74: перечень приезжал припиской внутри реплики ассистента, и модель
    в какой-то момент дописала такую же — не вызвав ничего. Человеку она при этом
    отчиталась о двух выполненных действиях.
    """
    policy.set_autonomy(db, member.family_id, 3)
    Agent(FakeLLM([_call("remember", text="Соня не ест грибы"),
                   LLMResponse(content="Запомнил.")])).respond(db, member, "запомни про Соню")

    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "ну ладно")

    for message in llm.calls[0]["messages"]:
        if message["role"] == "assistant":
            assert "remember(" not in message["content"]


def test_the_trail_never_reaches_the_human(db, member):
    """Служебная запись — для модели, а панель читает строку из базы."""
    policy.set_autonomy(db, member.family_id, 3)
    Agent(FakeLLM([_call("remember", text="Соня не ест грибы"),
                   LLMResponse(content="Запомнил.")])).respond(db, member, "запомни про Соню")

    saved = db.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
    assert saved.content == "Запомнил."


# --- выдуманные вызовы ----------------------------------------------------

FABRICATED = ("Готово, переименовала и запомнила рецепт.\n\n"
              "[что я тогда сделал:\nconfirm_meal(title=Огуречный суп) → done\n"
              "remember(text=рецепт) → done]")


def test_a_made_up_report_becomes_a_real_call(db, member):
    """Модель «отчиталась» о вызовах, не сделав их, — просим сделать по-настоящему."""
    policy.set_autonomy(db, member.family_id, 3)
    llm = FakeLLM([
        LLMResponse(content=FABRICATED),
        _call("remember", text="рецепт огуречного супа"),
        LLMResponse(content="Запомнила рецепт."),
    ])
    reply = Agent(llm).respond(db, member, "запомни этот рецепт")

    assert reply.text == "Запомнила рецепт."
    assert [t.status for t in reply.traces] == ["done"]
    assert db.query(BoardEntry).count() == 1
    # модели показали, что вышло, и попросили сделать
    assert any(m["role"] == "system" and m["content"].startswith("СТОП")
               for m in llm.calls[1]["messages"])


def test_a_made_up_report_never_reaches_the_human(db, member):
    """Даже если модель настаивает: перечень вызовов — не её слова.

    Срезать один перечень мало: слова над ним («переименовала и запомнила»)
    — такой же отчёт о несделанном, и человеку уходит правда вместо них.
    """
    policy.set_autonomy(db, member.family_id, 3)
    llm = FakeLLM([LLMResponse(content=FABRICATED), LLMResponse(content=FABRICATED)])
    reply = Agent(llm).respond(db, member, "запомни этот рецепт")

    assert reply.text == UNBACKED_REPLY
    assert "confirm_meal(" not in reply.text
    saved = db.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
    assert "remember(" not in saved.content
    assert "запомнила" not in saved.content


def test_a_made_up_report_stored_earlier_does_not_come_back_as_truth(db, member):
    """Строки, осевшие в базе до починки, в следующий ход едут без перечня."""
    from app.agent.runtime import save_message

    save_message(db, member, "assistant", FABRICATED)
    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "и что дальше?")

    said = [m["content"] for m in llm.calls[0]["messages"] if m["role"] == "assistant"][0]
    assert said == "Готово, переименовала и запомнила рецепт."


def test_a_refusal_is_remembered_too(db, member):
    """«Поиск не настроен» на прошлом ходу — повод не долбиться в него снова."""
    policy.set_autonomy(db, member.family_id, 3)
    asked = LLMResponse(tool_calls=[ToolCall(id="call_lookup", name="lookup_product",
                                             arguments={"name": "пицца Пепперони Додо 20 см"})])
    Agent(FakeLLM([asked, LLMResponse(content="Не нашёл состав.")])).respond(
        db, member, "а сколько в ней?")

    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "ну ладно")

    trail = [m["content"] for m in llm.calls[0]["messages"][1:] if m["role"] == "system"][0]
    assert "lookup_product(" in trail
    assert "failed" in trail


# --- отчёт о работе, которой не было --------------------------------------

CLAIMED = "Готово, мой хороший. Я записала эти две идеи на доску «Планы на развитие»."


def test_a_claim_without_a_call_becomes_a_real_call(db, member):
    """Прогон #81: «занесла на доску», ноль вызовов, на доске пусто.

    Перечня вызовов в таком ответе нет — есть одно «готово», и снаружи оно
    неотличимо от работы. Просим сделать по-настоящему.
    """
    policy.set_autonomy(db, member.family_id, 3)
    llm = FakeLLM([
        LLMResponse(content=CLAIMED),
        _call("remember", text="Максиму нравится идея графиков веса"),
        LLMResponse(content="Записала на доску."),
    ])
    reply = Agent(llm).respond(db, member, "занеси на доску")

    assert reply.text == "Записала на доску."
    assert [t.status for t in reply.traces] == ["done"]
    assert db.query(BoardEntry).count() == 1
    assert any(m["role"] == "system" and m["content"].startswith("СТОП")
               for m in llm.calls[1]["messages"])


def test_a_claim_the_model_insists_on_never_reaches_the_human(db, member):
    """Человеку уходит правда, а не слова о несделанном."""
    policy.set_autonomy(db, member.family_id, 3)
    llm = FakeLLM([LLMResponse(content=CLAIMED), LLMResponse(content=CLAIMED)])
    reply = Agent(llm).respond(db, member, "занеси на доску")

    assert reply.text == UNBACKED_REPLY
    assert "записала" not in reply.text.lower()
    assert db.query(BoardEntry).count() == 0
    saved = db.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
    assert saved.content == UNBACKED_REPLY


def test_a_real_call_leaves_the_report_alone(db, member):
    """«Записал» после настоящего вызова — правда, и трогать её нечего."""
    policy.set_autonomy(db, member.family_id, 3)
    llm = FakeLLM([_call("remember", text="Соня не ест грибы"),
                   LLMResponse(content="Готово, записала.")])
    reply = Agent(llm).respond(db, member, "запомни про Соню")

    assert reply.text == "Готово, записала."


def test_a_prepared_action_may_be_told_about(db, member):
    """«Подготовил и жду „да“» — не отчёт о сделанном, а честные слова."""
    policy.set_autonomy(db, member.family_id, 0)
    llm = FakeLLM([_call("remember", text="купить хлеб"),
                   LLMResponse(content="Подготовил действие, скажи «да».")])
    reply = Agent(llm).respond(db, member, "запомни купить хлеб")

    assert reply.text == "Подготовил действие, скажи «да»."


def test_words_without_work_are_left_alone(db, member):
    """Разговор без действий не трогаем: обмана в нём нет."""
    llm = FakeLLM([LLMResponse(content="Могу записать это на доску — сказать когда?")])
    reply = Agent(llm).respond(db, member, "а что ты умеешь?")

    assert reply.text == "Могу записать это на доску — сказать когда?"


def test_a_claim_stored_earlier_does_not_come_back_as_truth(db, member):
    """Прогон #82: своё вчерашнее «готово» модель читает как сделанное."""
    from app.agent.runtime import NOTHING_HAPPENED, save_message

    save_message(db, member, "assistant", CLAIMED)
    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "и что дальше?")

    messages = llm.calls[0]["messages"]
    assert [m["content"] for m in messages if m["role"] == "assistant"] == [CLAIMED]
    assert any(m["role"] == "system" and m["content"] == NOTHING_HAPPENED
               for m in messages[1:])


def test_a_reply_backed_by_a_call_is_not_marked_in_history(db, member):
    """Пометка — про пустой след, а не про слово «записал» само по себе."""
    policy.set_autonomy(db, member.family_id, 3)
    Agent(FakeLLM([_call("remember", text="Соня не ест грибы"),
                   LLMResponse(content="Готово, записала.")])).respond(db, member, "запомни")

    llm = FakeLLM([LLMResponse(content="Хорошо.")])
    Agent(llm).respond(db, member, "ну ладно")

    from app.agent.runtime import NOTHING_HAPPENED
    assert not any(m["content"] == NOTHING_HAPPENED
                   for m in llm.calls[0]["messages"] if m["role"] == "system")
