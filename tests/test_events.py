"""Конвейер событий: запись превращается в величины по словарю доски (тикет #30).

Разбор идёт при создании и при правке записи, а не батчем при сборке сводки:
иначе цифра за прошлый вторник менялась бы просто оттого, что сегодня модель
прочла лог иначе (ADR-0002). Запись при этом сохраняется немедленно — что бы
ни случилось с разбором.

Модель здесь не вызывается по-настоящему никогда: клиент подставной, как в
тестах оценки блюда.
"""
import pytest
from fastapi.testclient import TestClient

from app.agent.llm import LLMUnavailable
from app.agent.registry import ToolContext
from app.core.db import get_db
from app.main import app
from app.modules.memory import extraction, knowledge, tools
from tests.conftest import FakeLLM


def _parsed(*events):
    """Ответ модели: разбор записи на величины."""
    return {"events": [dict(event) for event in events]}


def _event(kind="кормление", at="2026-08-12 02:50", value=170, unit="мл",
           confidence="high", raw="02:50 170"):
    return {"kind": kind, "at": at, "value": value, "unit": unit,
            "confidence": confidence, "raw": raw}


@pytest.fixture
def board(db, head):
    """Доска со словарём типов: съеденное и потраченное — разные типы."""
    section = knowledge.create_section(db, head.id, "Малыш")
    board = knowledge.create_board(db, head.id, section.id, "Кормления",
                                   instruction="Записи вида «время объём»: 170 — это миллилитры.")
    knowledge.add_event_type(db, head.id, board.id, "кормление", "мл")
    knowledge.add_event_type(db, head.id, board.id, "срыгивание", "мл")
    return board


@pytest.fixture
def plain_board(db, head):
    """Доска без словаря: разбирать нечего и не во что."""
    section = knowledge.create_section(db, head.id, "Разное")
    return knowledge.create_board(db, head.id, section.id, "Мысли")


# --- разбор при создании, правке и удалении ------------------------------------

def test_an_entry_becomes_events_by_the_board_dictionary(db, head, board):
    llm = FakeLLM([_parsed(_event())])

    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170", llm=llm)

    events = knowledge.entry_events(db, entry.id)
    assert [(e.kind, e.value, e.unit) for e in events] == [("кормление", 170.0, "мл")]
    assert events[0].board_id == board.id


def test_editing_an_entry_reparses_its_events(db, head, board):
    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170",
                                llm=FakeLLM([_parsed(_event())]))

    knowledge.edit_entry(db, head.id, entry.id, "02:50 175",
                         llm=FakeLLM([_parsed(_event(value=175, raw="02:50 175"))]))

    assert [e.value for e in knowledge.entry_events(db, entry.id)] == [175.0]


def test_deleting_an_entry_takes_its_events_with_it(db, head, board):
    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170",
                                llm=FakeLLM([_parsed(_event())]))

    assert knowledge.delete_entry(db, head.id, entry.id)
    assert knowledge.entry_events(db, entry.id) == []


def test_the_entry_survives_a_model_that_did_not_answer(db, head, board):
    """Запись сохраняется немедленно и независимо от результата разбора."""
    class Dead:
        def json_completion(self, *args, **kwargs):
            raise LLMUnavailable("модель недоступна")

    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170", llm=Dead())

    assert entry is not None
    assert [e.text for e in knowledge.list_entries(db, head.id, board.id)] == ["02:50 170"]
    assert knowledge.entry_events(db, entry.id) == []


def test_a_board_without_a_dictionary_is_not_sent_to_the_model(db, head, plain_board):
    """Тип берётся из словаря доски: нет словаря — нет и разбора, и вызова модели."""
    llm = FakeLLM([])

    entry = knowledge.add_entry(db, head.id, plain_board.id, "просто мысль", llm=llm)

    assert entry is not None
    assert llm.calls == []
    assert knowledge.entry_events(db, entry.id) == []


def test_an_entry_written_by_the_assistant_is_parsed_too(db, head, board, monkeypatch):
    """Разбор не зависит от пути записи: панель и write_entry дают одно и то же."""
    llm = FakeLLM([_parsed(_event())])
    monkeypatch.setattr(extraction, "default_client", llm)

    result = tools.write_entry(ToolContext(db=db, actor=head, subject=head),
                               board="Кормления", text="02:50 170")

    assert result.ok
    events = knowledge.entry_events(db, result.data["entry_id"])
    assert [(e.kind, e.value) for e in events] == [("кормление", 170.0)]


# --- неуверенное не идёт в сумму ------------------------------------------------

def _totals(db, user_id, board_id):
    return {(row["kind"], row["unit"]): row["total"]
            for row in knowledge.event_totals(db, user_id, board_id)}


def test_an_uncertain_value_stays_out_of_the_sum(db, head, board):
    knowledge.add_entry(db, head.id, board.id, "02:50 170", llm=FakeLLM([_parsed(_event())]))
    knowledge.add_entry(db, head.id, board.id, "потом ещё немного 40",
                        llm=FakeLLM([_parsed(_event(value=40, confidence="low", raw="40"))]))

    assert _totals(db, head.id, board.id) == {("кормление", "мл"): 170.0}


def test_another_unit_is_a_row_of_its_own_and_not_a_lump(db, head, board):
    """«170 мл» и «0.2 л» — две строки: сумма, неизвестно в чём, хуже двух известных."""
    knowledge.add_entry(db, head.id, board.id, "02:50 170", llm=FakeLLM([_parsed(_event())]))
    knowledge.add_entry(db, head.id, board.id, "днём 0.2 л",
                        llm=FakeLLM([_parsed(_event(value=0.2, unit="л", raw="0.2 л"))]))

    assert _totals(db, head.id, board.id) == {("кормление", "мл"): 170.0,
                                              ("кормление", "л"): 0.2}


def test_the_unit_of_the_dictionary_is_written_the_way_the_dictionary_writes_it(db, head, board):
    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170",
                                llm=FakeLLM([_parsed(_event(unit="МЛ"))]))

    assert knowledge.entry_events(db, entry.id)[0].unit == "мл"


def test_a_kind_outside_the_dictionary_is_uncertain(db, head, board):
    """«Кормление», «еда» и «молоко» не заводятся вперемешку: чужое имя — повод переспросить."""
    entry = knowledge.add_entry(db, head.id, board.id, "молока 170",
                                llm=FakeLLM([_parsed(_event(kind="молоко"))]))

    assert knowledge.entry_events(db, entry.id)[0].confidence == extraction.LOW
    assert _totals(db, head.id, board.id) == {}


def test_a_model_that_did_not_answer_does_not_erase_what_was_already_parsed(db, head, board):
    """Молчание модели — не повод стирать уточнённое: правка вернётся к разбору позже."""
    class Dead:
        def json_completion(self, *args, **kwargs):
            raise LLMUnavailable("модель недоступна")

    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170",
                                llm=FakeLLM([_parsed(_event())]))

    knowledge.edit_entry(db, head.id, entry.id, "02:50 175", llm=Dead())

    assert entry.text == "02:50 175"
    assert [e.value for e in knowledge.entry_events(db, entry.id)] == [170.0]


def test_a_signed_number_is_not_a_type(db, head, board):
    """Съеденное и потраченное — разные типы, а не число со знаком."""
    entry = knowledge.add_entry(db, head.id, board.id, "срыгнул 30",
                                llm=FakeLLM([_parsed(_event(kind="срыгивание", value=-30))]))

    event = knowledge.entry_events(db, entry.id)[0]
    assert event.value == 30.0
    assert event.confidence == extraction.LOW


def test_a_value_that_is_not_a_number_is_not_an_event(db, head, board):
    entry = knowledge.add_entry(db, head.id, board.id, "покормила",
                                llm=FakeLLM([_parsed(_event(value="сколько-то"))]))

    assert knowledge.entry_events(db, entry.id) == []


# --- извлечение как функция с внедряемым клиентом --------------------------------

def test_the_extractor_shows_the_model_the_dictionary_and_the_instruction(db, head, board):
    llm = FakeLLM([_parsed(_event())])

    events = extraction.extract_events(
        "02:50 170", instruction=board.instruction,
        types=knowledge.list_event_types(db, board.id), at=None, llm=llm)

    asked = llm.calls[0]["user"]
    assert "кормление" in asked and "мл" in asked
    assert "170 — это миллилитры" in asked          # инструкция доски
    assert "02:50 170" in asked                     # сам текст записи
    assert [e.kind for e in events] == ["кормление"]


# --- плашка уточнения на экране --------------------------------------------------

@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_head(client, head):
    client.post("/login", data={"username": head.username, "password": "pw"},
                follow_redirects=False)
    return client


def board_url(board):
    return f"/memory?section={board.section_id}&board={board.id}"


def test_the_entry_is_saved_and_the_clarification_waits_quietly(db, head, board, as_head,
                                                                monkeypatch):
    monkeypatch.setattr(extraction, "default_client",
                        FakeLLM([_parsed(_event(kind="молоко", confidence="low", raw="170"))]))

    as_head.post("/memory/entries/add", data={"board_id": board.id, "text": "02:50 170"},
                 follow_redirects=False)
    page = as_head.get(board_url(board))

    assert "02:50 170" in page.text                  # запись уже на доске
    assert "data-clarify" in page.text               # и под ней тихая плашка
    assert "кормление" in page.text and "срыгивание" in page.text   # варианты из словаря
    assert 'name="own"' in page.text                 # и поле свободного ответа


def test_the_answer_puts_the_value_back_into_the_sum(db, head, board, as_head, monkeypatch):
    monkeypatch.setattr(extraction, "default_client",
                        FakeLLM([_parsed(_event(kind="молоко", confidence="low"))]))
    as_head.post("/memory/entries/add", data={"board_id": board.id, "text": "02:50 170"},
                 follow_redirects=False)
    event = knowledge.board_events(db, board.id)[0]

    as_head.post(f"/memory/events/{event.id}/clarify", data={"kind": "кормление", "own": ""},
                 follow_redirects=False)

    assert _totals(db, head.id, board.id) == {("кормление", "мл"): 170.0}
    assert "data-clarify" not in as_head.get(board_url(board)).text


def test_an_answer_in_your_own_words_adds_a_type_to_the_dictionary(db, head, board):
    entry = knowledge.add_entry(db, head.id, board.id, "гулял 40",
                                llm=FakeLLM([_parsed(_event(kind="кормление", value=40,
                                                            unit="мин", confidence="low"))]))
    event = knowledge.entry_events(db, entry.id)[0]

    assert knowledge.clarify_event(db, head.id, event.id, "прогулка")

    assert event.kind == "прогулка"
    assert [t.name for t in knowledge.list_event_types(db, board.id)] == [
        "кормление", "срыгивание", "прогулка"]
    assert _totals(db, head.id, board.id) == {("прогулка", "мин"): 40.0}


def test_a_stranger_does_not_answer_for_your_board(db, head, member, board):
    entry = knowledge.add_entry(db, head.id, board.id, "02:50 170",
                                llm=FakeLLM([_parsed(_event(confidence="low"))]))
    event = knowledge.entry_events(db, entry.id)[0]

    assert not knowledge.clarify_event(db, member.id, event.id, "срыгивание")
    assert event.kind == "кормление"
    assert event.confidence == extraction.LOW


def test_the_dictionary_of_a_board_starts_in_the_panel(db, head, plain_board, as_head,
                                                       monkeypatch):
    """Пока величин нет, доска не разбирается вовсе; заводят их руками на доске."""
    as_head.post(f"/memory/boards/{plain_board.id}/types/add",
                 data={"name": "показание", "unit": "кВт"}, follow_redirects=False)

    assert [(t.name, t.unit) for t in knowledge.list_event_types(db, plain_board.id)] == [
        ("показание", "кВт")]

    monkeypatch.setattr(extraction, "default_client", FakeLLM([
        _parsed(_event(kind="показание", value=1200, unit="кВт", raw="1200"))]))
    as_head.post("/memory/entries/add", data={"board_id": plain_board.id, "text": "за март 1200"},
                 follow_redirects=False)

    assert _totals(db, head.id, plain_board.id) == {("показание", "кВт"): 1200.0}


def test_a_stranger_does_not_touch_the_dictionary_of_your_board(db, head, member, board):
    assert knowledge.add_event_type(db, member.id, board.id, "своё", "шт") is None
    assert [t.name for t in knowledge.list_event_types(db, board.id)] == ["кормление", "срыгивание"]
