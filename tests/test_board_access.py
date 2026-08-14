"""Доступ к доскам: шаринг, «Общее» и единая точка разрешения (тикет #28).

Весь инвариант приватности — одна функция сервиса знаний: «какие доски видит
этот участник и с каким правом». Права ровно два — просмотр и редактирование
(свои записи); чужие записи правит только владелец. «Всем» — живое правило.
«Общее» вычисляется из грантов, а не хранится строкой.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.db import get_db
from app.core.models import ROLE_MEMBER, User
from app.main import app
from app.modules.memory import knowledge
from app.modules.memory.models import BoardEntry, RIGHT_EDIT, RIGHT_VIEW


@pytest.fixture
def their_board(db, other):
    """Доска участника, которой он будет делиться с главой семьи."""
    section = knowledge.create_section(db, other.id, "Лёвино")
    return knowledge.create_board(db, other.id, section.id, "Продукты",
                                  instruction="список покупок")


# --- единая точка разрешения ---------------------------------------------------

def test_the_resolution_point_sees_own_named_and_family_wide_boards(db, member, other, their_board):
    own_section = knowledge.create_section(db, member.id, "Дом")
    own = knowledge.create_board(db, member.id, own_section.id, "Счётчики")
    private_section = knowledge.create_section(db, other.id, "Личное")
    knowledge.create_board(db, other.id, private_section.id, "Дневник")
    family_wide = knowledge.create_board(db, other.id, private_section.id, "Аптечка")

    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)
    knowledge.share_board_with_all(db, other.id, family_wide.id, RIGHT_EDIT)

    grants = {g.board.name: g.right for g in knowledge.board_grants(db, member.id)}
    assert grants == {"Счётчики": knowledge.RIGHT_OWNER,
                      "Продукты": RIGHT_VIEW,
                      "Аптечка": RIGHT_EDIT}


def test_all_is_a_living_rule_for_a_new_family_member(db, family, other, their_board):
    knowledge.share_board_with_all(db, other.id, their_board.id, RIGHT_VIEW)

    newcomer = User(family_id=family.id, username="sonya", display_name="Соня",
                    relation="дочь", role=ROLE_MEMBER, avatar_slot=2)
    db.add(newcomer)
    db.commit()

    grants = [g.board.name for g in knowledge.board_grants(db, newcomer.id)]
    assert grants == ["Продукты"]


def test_edit_wins_when_named_and_family_wide_rights_disagree(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)
    knowledge.share_board_with_all(db, other.id, their_board.id, RIGHT_EDIT)

    grant = knowledge.board_access(db, member.id, their_board.id)
    assert grant.right == RIGHT_EDIT


def test_sharing_is_the_owners_move_alone(db, member, other, their_board):
    assert not knowledge.share_board(db, member.id, their_board.id, member.id, RIGHT_VIEW)
    assert not knowledge.share_board_with_all(db, member.id, their_board.id, RIGHT_VIEW)
    assert knowledge.board_access(db, member.id, their_board.id) is None


def test_common_collects_shared_boards_by_freshness(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)

    shared = knowledge.shared_boards(db, member.id)
    assert [g.board.name for g in shared] == ["Продукты"]


# --- права на записи -------------------------------------------------------------

def test_a_viewer_reads_but_cannot_write(db, member, other, their_board):
    knowledge.add_entry(db, other.id, their_board.id, "молоко, хлеб")
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)

    assert [e.text for e in knowledge.list_entries(db, member.id, their_board.id)] == ["молоко, хлеб"]
    assert knowledge.add_entry(db, member.id, their_board.id, "и батарейки") is None


def test_an_editor_writes_and_edits_only_their_own(db, member, other, their_board):
    owners_entry = knowledge.add_entry(db, other.id, their_board.id, "молоко")
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_EDIT)

    mine = knowledge.add_entry(db, member.id, their_board.id, "и батарейки")
    assert mine is not None
    assert knowledge.edit_entry(db, member.id, mine.id, "и батарейки AA") is not None

    assert knowledge.edit_entry(db, member.id, owners_entry.id, "переписал чужое") is None
    assert not knowledge.delete_entry(db, member.id, owners_entry.id)


def test_the_owner_edits_any_entry_on_their_board(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_EDIT)
    guests_entry = knowledge.add_entry(db, member.id, their_board.id, "и батарейки")

    assert knowledge.edit_entry(db, other.id, guests_entry.id, "батарейки AAA") is not None
    assert knowledge.delete_entry(db, other.id, guests_entry.id)


def test_a_viewer_cannot_edit_even_what_they_wrote_before_the_downgrade(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_EDIT)
    old_mine = knowledge.add_entry(db, member.id, their_board.id, "моё раннее")

    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)

    assert knowledge.edit_entry(db, member.id, old_mine.id, "правлю") is None


# --- отзыв и блокировки -----------------------------------------------------------

def test_revoking_hides_the_board_but_keeps_the_entries(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_EDIT)
    knowledge.add_entry(db, member.id, their_board.id, "запись гостя")

    knowledge.revoke_share(db, other.id, their_board.id, member.id)

    assert knowledge.board_access(db, member.id, their_board.id) is None
    texts = [e.text for e in knowledge.list_entries(db, other.id, their_board.id)]
    assert "запись гостя" in texts


def test_deleting_a_board_with_active_access_is_blocked(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)

    with pytest.raises(knowledge.ActiveShares):
        knowledge.delete_board(db, other.id, their_board.id)

    knowledge.revoke_share(db, other.id, their_board.id, member.id)
    assert knowledge.delete_board(db, other.id, their_board.id)


def test_deleting_a_section_with_a_shared_board_is_blocked(db, member, other, their_board):
    knowledge.share_board_with_all(db, other.id, their_board.id, RIGHT_VIEW)

    with pytest.raises(knowledge.ActiveShares):
        knowledge.delete_section(db, other.id, their_board.section_id)

    knowledge.stop_sharing_with_all(db, other.id, their_board.id)
    assert knowledge.delete_section(db, other.id, their_board.section_id)


def test_the_audience_is_visible_to_anyone_with_access(db, member, other, their_board):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)

    audience = knowledge.board_audience(db, member.id, their_board.id)

    assert audience["owner"].id == other.id
    assert [(u.id, right) for u, right in audience["shares"]] == [(member.id, RIGHT_VIEW)]
    assert audience["all_right"] is None


# --- экран -----------------------------------------------------------------------

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


def test_a_shared_board_lives_in_common_and_the_viewer_knows_who_to_ask(db, member, other,
                                                                        their_board, as_member):
    knowledge.add_entry(db, other.id, their_board.id, "молоко, хлеб")
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_VIEW)

    common = as_member.get("/memory?section=common")
    assert "Продукты" in common.text

    page = as_member.get(f"/memory?section=common&board={their_board.id}")
    assert "молоко, хлеб" in page.text
    assert "список покупок" in page.text            # инструкция видна допущенному
    assert "Написать на доску" not in page.text     # поля ввода нет
    assert other.display_name in page.text          # к кому идти за доступом


def test_an_editor_gets_the_composer_in_common(db, member, other, their_board, as_member):
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_EDIT)

    page = as_member.get(f"/memory?section=common&board={their_board.id}")

    assert "Написать на доску" in page.text


def test_share_and_revoke_work_from_the_screen(db, member, other, as_member):
    section = knowledge.create_section(db, member.id, "Дом")
    board = knowledge.create_board(db, member.id, section.id, "Счётчики")

    as_member.post(f"/memory/boards/{board.id}/share",
                 data={"member_id": other.id, "right": RIGHT_EDIT},
                 follow_redirects=False)
    assert knowledge.board_access(db, other.id, board.id).right == RIGHT_EDIT

    as_member.post(f"/memory/boards/{board.id}/unshare", data={"member_id": other.id},
                 follow_redirects=False)
    assert knowledge.board_access(db, other.id, board.id) is None


def test_a_guests_delete_attempt_returns_them_to_common(db, member, other, their_board, as_member):
    """Не владелец доску не сносит — и не выпадает при этом на корневой экран."""
    knowledge.share_board(db, other.id, their_board.id, member.id, RIGHT_EDIT)

    response = as_member.post(f"/memory/boards/{their_board.id}/delete", follow_redirects=False)

    assert response.headers["location"] == f"/memory?section=common&board={their_board.id}"
    assert knowledge.board_access(db, member.id, their_board.id) is not None


def test_a_blocked_delete_explains_itself_on_the_screen(db, member, other, as_member):
    section = knowledge.create_section(db, member.id, "Дом")
    board = knowledge.create_board(db, member.id, section.id, "Счётчики")
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)

    response = as_member.post(f"/memory/boards/{board.id}/delete", follow_redirects=False)
    page = as_member.get(response.headers["location"])

    assert knowledge.board_access(db, member.id, board.id) is not None   # жива
    assert "отзовите доступ" in page.text.lower()
