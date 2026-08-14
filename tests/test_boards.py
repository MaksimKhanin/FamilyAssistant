"""Доски внутри раздела: лента с инструкцией ассистенту (тикет #26, спека #19).

Доска принадлежит владельцу раздела и не дублирует владельца в своей строке —
он вычисляется через раздел, поэтому перенос доски между разделами не может
разъехаться с правами. Лента записей — предмет #27; здесь список досок,
инструкция в шапке и перенос между своими разделами.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app
from app.modules.memory import knowledge
from app.modules.memory.models import Board, BoardEntry


# --- сервис -------------------------------------------------------------------

def test_the_owner_creates_a_board_with_an_instruction(db, member):
    section = knowledge.create_section(db, member.id, "Дети")

    board = knowledge.create_board(db, member.id, section.id, "Питание и сон",
                                   instruction="19.50 170 — время и миллилитры")

    assert board.section_id == section.id
    assert board.instruction == "19.50 170 — время и миллилитры"


def test_a_board_cannot_land_in_someone_elses_section(db, member, other):
    foreign = knowledge.create_section(db, other.id, "Лёвино")

    assert knowledge.create_board(db, member.id, foreign.id, "Чужая") is None
    assert db.query(Board).count() == 0


def test_boards_go_by_freshness(db, member):
    section = knowledge.create_section(db, member.id, "Дети")
    stale = knowledge.create_board(db, member.id, section.id, "Старая")
    fresh = knowledge.create_board(db, member.id, section.id, "Свежая")
    now = datetime.utcnow()
    stale.last_activity_at = now - timedelta(days=7)
    fresh.last_activity_at = now
    db.commit()

    names = [b.name for b in knowledge.list_boards(db, member.id, section.id)]
    assert names == ["Свежая", "Старая"]


def test_creating_a_board_freshens_its_section(db, member):
    dormant = knowledge.create_section(db, member.id, "Спящий")
    active = knowledge.create_section(db, member.id, "Живой")
    dormant.last_activity_at = datetime.utcnow() - timedelta(days=7)
    active.last_activity_at = datetime.utcnow() - timedelta(days=5)
    db.commit()

    knowledge.create_board(db, member.id, dormant.id, "Новая доска")

    names = [s.name for s in knowledge.list_sections(db, member.id)]
    assert names == ["Спящий", "Живой"]


def test_only_the_owner_edits_name_and_instruction(db, member, other):
    section = knowledge.create_section(db, other.id, "Лёвино")
    board = knowledge.create_board(db, other.id, section.id, "Дневник",
                                   instruction="как есть")

    assert knowledge.update_board(db, member.id, board.id, name="Захвачено",
                                  instruction="иначе") is None
    assert (board.name, board.instruction) == ("Дневник", "как есть")

    knowledge.update_board(db, other.id, board.id, name="Дневник сна",
                           instruction="время и часы сна")
    assert (board.name, board.instruction) == ("Дневник сна", "время и часы сна")


def test_a_board_moves_only_between_own_sections(db, member, other):
    source = knowledge.create_section(db, member.id, "Дом")
    target = knowledge.create_section(db, member.id, "Ремонт")
    foreign = knowledge.create_section(db, other.id, "Лёвино")
    board = knowledge.create_board(db, member.id, source.id, "Счётчики")

    assert knowledge.move_board(db, member.id, board.id, foreign.id) is None
    assert board.section_id == source.id

    knowledge.move_board(db, member.id, board.id, target.id)
    assert board.section_id == target.id


def test_a_moved_board_takes_its_activity_with_it(db, member):
    """Полоса разделов сортируется по денормализованной активности — перенос
    доски пересчитывает её и у источника, и у цели."""
    now = datetime.utcnow()
    source = knowledge.create_section(db, member.id, "Дом")
    target = knowledge.create_section(db, member.id, "Ремонт")
    board = knowledge.create_board(db, member.id, source.id, "Счётчики")
    board.last_activity_at = now
    source.created_at = source.last_activity_at = now - timedelta(days=7)
    target.created_at = target.last_activity_at = now - timedelta(days=5)
    db.commit()

    knowledge.move_board(db, member.id, board.id, target.id)

    names = [s.name for s in knowledge.list_sections(db, member.id)]
    assert names == ["Ремонт", "Дом"]


def test_a_save_with_a_foreign_target_section_changes_nothing(db, member, other):
    """Правка и перенос — одна транзакция: частичного сохранения не бывает."""
    section = knowledge.create_section(db, member.id, "Дом")
    foreign = knowledge.create_section(db, other.id, "Лёвино")
    board = knowledge.create_board(db, member.id, section.id, "Счётчики")

    result = knowledge.update_board(db, member.id, board.id, name="Переписано",
                                    section_id=foreign.id)

    assert result is None
    assert (board.name, board.section_id) == ("Счётчики", section.id)


def test_deleting_a_board_takes_its_entries_with_it(db, member):
    """Блокировка удаления при активном доступе появится с шарингом (#28)."""
    section = knowledge.create_section(db, member.id, "Дети")
    board = knowledge.create_board(db, member.id, section.id, "Питание")
    db.add(BoardEntry(board_id=board.id, author_id=member.id, text="12-20 170 мл"))
    db.commit()

    assert knowledge.delete_board(db, member.id, board.id)
    assert db.query(BoardEntry).count() == 0


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


def test_the_section_screen_lists_boards_and_keeps_the_instruction_in_the_header(db, member, as_member):
    section = knowledge.create_section(db, member.id, "Дети")
    board = knowledge.create_board(db, member.id, section.id, "Питание и сон",
                                   instruction="19.50 170 — время и миллилитры")
    knowledge.create_board(db, member.id, section.id, "Садик")

    page = as_member.get(f"/memory?section={section.id}&board={board.id}")

    assert page.status_code == 200
    assert "Питание и сон" in page.text
    assert "Садик" in page.text
    assert "19.50 170 — время и миллилитры" in page.text


def test_a_board_can_be_added_from_the_screen(db, member, as_member):
    section = knowledge.create_section(db, member.id, "Дети")

    response = as_member.post("/memory/boards/add",
                            data={"section_id": section.id, "name": "Питание",
                                  "instruction": "время и миллилитры"},
                            follow_redirects=False)

    assert response.status_code == 303
    boards = knowledge.list_boards(db, member.id, section.id)
    assert [(b.name, b.instruction) for b in boards] == [("Питание", "время и миллилитры")]


def test_edit_move_and_delete_work_from_the_screen(db, member, as_member):
    source = knowledge.create_section(db, member.id, "Дом")
    target = knowledge.create_section(db, member.id, "Ремонт")
    board = knowledge.create_board(db, member.id, source.id, "Счётчики")

    as_member.post(f"/memory/boards/{board.id}/update",
                 data={"name": "Счётчики воды", "instruction": "показания по датам",
                       "section_id": target.id},
                 follow_redirects=False)
    db.expire_all()
    assert (board.name, board.instruction, board.section_id) == \
        ("Счётчики воды", "показания по датам", target.id)

    as_member.post(f"/memory/boards/{board.id}/delete", follow_redirects=False)
    assert db.query(Board).count() == 0


def test_someone_elses_board_is_not_reachable_over_http(db, member, other, as_member):
    foreign_section = knowledge.create_section(db, other.id, "Лёвино")
    foreign = knowledge.create_board(db, other.id, foreign_section.id, "Личный дневник",
                                     instruction="никому не показывать")
    own = knowledge.create_section(db, member.id, "Дом")

    page = as_member.get(f"/memory?section={own.id}&board={foreign.id}")
    assert "Личный дневник" not in page.text
    assert "никому не показывать" not in page.text

    as_member.post(f"/memory/boards/{foreign.id}/delete", follow_redirects=False)
    assert db.query(Board).count() == 1
