"""Лента записей доски: автор, время, правка с пометкой (тикет #27, спека #19).

Лента — совместный лог: у каждой записи видны автор и время, подряд идущие
записи одного автора не склеиваются (время каждой — это данные), правка не
тихая («изменено»), записи не закрепляются. Запись переживает автора
(ADR-0004): «Ассистент» и «бывший участник» различаются парой полей.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app
from app.modules.memory import knowledge
from app.modules.memory.models import BoardEntry


@pytest.fixture
def board(db, member):
    section = knowledge.create_section(db, member.id, "Дети")
    return knowledge.create_board(db, member.id, section.id, "Питание и сон",
                                  instruction="время и миллилитры")


# --- сервис -------------------------------------------------------------------

def test_an_entry_lands_in_the_feed_with_author_and_time(db, member, board):
    entry = knowledge.add_entry(db, member.id, board.id, "12-20 170 мл")

    assert entry.author_id == member.id
    assert not entry.by_assistant
    assert entry.edited_at is None
    assert [e.text for e in knowledge.list_entries(db, member.id, board.id)] == ["12-20 170 мл"]


def test_a_blank_entry_is_not_added(db, member, board):
    assert knowledge.add_entry(db, member.id, board.id, "   ") is None
    assert knowledge.list_entries(db, member.id, board.id) == []


def test_an_entry_freshens_its_board_and_section(db, member, board):
    stale = datetime.utcnow() - timedelta(days=7)
    section = knowledge.get_section(db, member.id, board.section_id)
    board.last_activity_at = stale
    section.last_activity_at = stale
    db.commit()

    knowledge.add_entry(db, member.id, board.id, "новая запись")

    assert board.last_activity_at > stale
    assert section.last_activity_at > stale


def test_editing_changes_the_text_and_marks_it(db, member, board):
    entry = knowledge.add_entry(db, member.id, board.id, "12-20 170 мл")

    knowledge.edit_entry(db, member.id, entry.id, "12-20 175 мл")

    assert entry.text == "12-20 175 мл"
    assert entry.edited_at is not None


def test_deleting_removes_the_entry(db, member, board):
    entry = knowledge.add_entry(db, member.id, board.id, "лишнее")

    assert knowledge.delete_entry(db, member.id, entry.id)
    assert knowledge.list_entries(db, member.id, board.id) == []


def test_entries_on_someone_elses_board_are_out_of_reach(db, member, other, board):
    entry = knowledge.add_entry(db, member.id, board.id, "чужое не трогать")

    assert knowledge.add_entry(db, other.id, board.id, "вклинился") is None
    assert knowledge.edit_entry(db, other.id, entry.id, "переписал") is None
    assert not knowledge.delete_entry(db, other.id, entry.id)
    assert knowledge.list_entries(db, other.id, board.id) == []
    assert entry.text == "чужое не трогать"


def test_entries_have_no_pinning(db):
    """Закрепление есть у разделов; лента доски по своей природе лог."""
    assert not hasattr(BoardEntry, "pinned")


# --- экран --------------------------------------------------------------------

@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_member(client, member):
    client.post("/login", data={"username": member.username, "password": "pw"},
                follow_redirects=False)
    return client


def board_url(board):
    return f"/memory?section={board.section_id}&board={board.id}"


def test_the_feed_shows_author_time_and_day_separators(db, member, board, as_member):
    yesterday = knowledge.add_entry(db, member.id, board.id, "вчерашние 170 мл")
    yesterday.created_at = datetime.utcnow() - timedelta(days=1)
    db.commit()
    knowledge.add_entry(db, member.id, board.id, "сегодняшние 160 мл")

    page = as_member.get(board_url(board))

    assert "вчерашние 170 мл" in page.text
    assert "Вчера" in page.text
    assert "Сегодня" in page.text
    assert page.text.count('class="entry-author"') == 2   # автор у каждой записи


def test_consecutive_entries_of_one_author_do_not_merge(db, member, board, as_member):
    knowledge.add_entry(db, member.id, board.id, "12-20 170 мл")
    knowledge.add_entry(db, member.id, board.id, "16-05 170 мл")

    page = as_member.get(board_url(board))

    assert page.text.count('class="entry-author"') == 2
    assert page.text.count("data-entry-id") == 2


def test_the_assistant_and_the_gone_author_are_labelled(db, member, board, as_member):
    db.add_all([
        BoardEntry(board_id=board.id, author_id=None, by_assistant=True, text="Соня не ест грибы"),
        BoardEntry(board_id=board.id, author_id=None, by_assistant=False, text="термометр в ящике"),
    ])
    db.commit()

    page = as_member.get(board_url(board))

    assert "Ассистент" in page.text
    assert "бывший участник" in page.text


def test_an_entry_can_be_added_from_the_screen(db, member, board, as_member):
    response = as_member.post("/memory/entries/add",
                            data={"board_id": board.id, "text": "20.00 - 170 мл"},
                            follow_redirects=False)

    assert response.status_code == 303
    assert [e.text for e in knowledge.list_entries(db, member.id, board.id)] == ["20.00 - 170 мл"]


def test_an_edited_entry_is_marked_on_the_screen(db, member, board, as_member):
    entry = knowledge.add_entry(db, member.id, board.id, "12-20 170 мл")

    as_member.post(f"/memory/entries/{entry.id}/edit", data={"text": "12-20 175 мл"},
                 follow_redirects=False)
    page = as_member.get(board_url(board))

    assert "12-20 175 мл" in page.text
    assert "изменено" in page.text


def test_an_entry_can_be_deleted_from_the_screen(db, member, board, as_member):
    entry = knowledge.add_entry(db, member.id, board.id, "лишнее")

    as_member.post(f"/memory/entries/{entry.id}/delete", follow_redirects=False)

    assert knowledge.list_entries(db, member.id, board.id) == []


def test_a_rejected_edit_still_returns_to_the_board(db, member, board, as_member):
    """Пустая правка отклоняется, но человека не выбрасывает с доски."""
    entry = knowledge.add_entry(db, member.id, board.id, "12-20 170 мл")

    response = as_member.post(f"/memory/entries/{entry.id}/edit", data={"text": "   "},
                            follow_redirects=False)

    assert response.headers["location"] == board_url(board)
    assert entry.text == "12-20 170 мл"


def test_same_calendar_dates_of_different_years_do_not_merge(db, member, board, as_member):
    """Подпись дня без года — но группировка идёт по дате, а не по подписи."""
    old = knowledge.add_entry(db, member.id, board.id, "позапрошлогоднее")
    old.created_at = datetime.utcnow() - timedelta(days=730)
    recent = knowledge.add_entry(db, member.id, board.id, "прошлогоднее тех же чисел")
    recent.created_at = old.created_at + timedelta(days=365, hours=1)
    db.commit()

    page = as_member.get(board_url(board))

    assert page.text.count('class="daysep"') == 2


def test_a_foreign_feed_is_closed_over_http(db, member, other, as_member):
    foreign_section = knowledge.create_section(db, other.id, "Лёвино")
    foreign = knowledge.create_board(db, other.id, foreign_section.id, "Дневник")
    entry = knowledge.add_entry(db, other.id, foreign.id, "личное")

    as_member.post("/memory/entries/add", data={"board_id": foreign.id, "text": "вклинился"},
                 follow_redirects=False)
    as_member.post(f"/memory/entries/{entry.id}/delete", follow_redirects=False)

    assert [e.text for e in knowledge.list_entries(db, other.id, foreign.id)] == ["личное"]
