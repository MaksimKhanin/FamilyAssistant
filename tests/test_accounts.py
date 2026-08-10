"""Учётные записи: кто кого может завести, разжаловать и удалить."""
import pytest

from app.core import accounts
from app.core.accounts import AccountError
from app.core.auth import hash_password, verify_password
from app.core.models import ROLE_HEAD, ROLE_MEMBER, User
from app.modules.memory import service as memory
from app.modules.nutrition import service as nutrition
from app.modules.nutrition.vision import MealEstimate


# --- создание -------------------------------------------------------------

def test_head_adds_a_member_who_will_set_their_own_password(db, head):
    member = accounts.create_member(db, head, "Соня", "дочь")

    assert member.username == "sonya"
    assert member.role == ROLE_MEMBER
    assert member.invite_code                 # ссылка выдана
    assert member.password_hash is None       # пароль придумает сам


def test_member_cannot_add_anyone(db, member):
    with pytest.raises(AccountError, match="глава семьи"):
        accounts.create_member(db, member, "Кто-то")


def test_login_is_derived_from_the_name_and_never_collides(db, head):
    first = accounts.create_member(db, head, "Лёва")
    second = accounts.create_member(db, head, "Лёва")

    assert first.username == "leva"
    assert second.username == "leva2"


def test_nameless_member_is_refused(db, head):
    with pytest.raises(AccountError, match="имени"):
        accounts.create_member(db, head, "   ")


def test_family_size_is_bounded(db, head, monkeypatch):
    monkeypatch.setattr(accounts, "MAX_MEMBERS", 2)
    accounts.create_member(db, head, "Соня")
    with pytest.raises(AccountError, match="не семья"):
        accounts.create_member(db, head, "Лёва")


# --- роли -----------------------------------------------------------------

def test_head_can_promote_someone(db, head, member):
    accounts.set_head(db, head, member.id, True)
    assert member.role == ROLE_HEAD


def test_nobody_changes_their_own_role(db, head):
    """Единственный админ не должен уметь запереть себя снаружи."""
    with pytest.raises(AccountError, match="Свою роль"):
        accounts.set_head(db, head, head.id, False)


def test_a_second_head_can_demote_the_first(db, head, member):
    accounts.set_head(db, head, member.id, True)
    accounts.set_head(db, member, head.id, False)

    assert head.role == ROLE_MEMBER
    assert member.role == ROLE_HEAD


# --- приглашения ----------------------------------------------------------

def test_reissuing_the_link_invalidates_the_old_one_and_the_password(db, head, member):
    member.password_hash = hash_password("старый-пароль")
    member.invite_code = "старый-код"
    db.commit()

    accounts.issue_invite(db, head, member.id)

    assert member.invite_code != "старый-код"
    assert member.password_hash is None
    assert not verify_password("старый-пароль", member.password_hash)


def test_revoking_a_link_leaves_an_existing_login_alone(db, head, member):
    member.password_hash = hash_password("рабочий-пароль")
    member.invite_code = "код"
    db.commit()

    accounts.revoke_invite(db, head, member.id)

    assert member.invite_code is None
    assert verify_password("рабочий-пароль", member.password_hash)


def test_member_cannot_reset_anyones_password(db, member, head):
    with pytest.raises(AccountError):
        accounts.issue_invite(db, member, head.id)


def test_head_cannot_reset_their_own_password_this_way(db, head):
    """Иначе единственный админ стирал бы себе пароль и оставался снаружи."""
    with pytest.raises(AccountError, match="в профиле"):
        accounts.issue_invite(db, head, head.id)

    assert head.password_hash is not None


# --- свой пароль ----------------------------------------------------------

def test_changing_own_password(db, head):
    accounts.change_own_password(db, head, "pw", "новый-пароль", "новый-пароль")

    assert verify_password("новый-пароль", head.password_hash)
    assert not verify_password("pw", head.password_hash)


def test_wrong_current_password_changes_nothing(db, head):
    with pytest.raises(AccountError, match="Текущий пароль"):
        accounts.change_own_password(db, head, "не тот", "новый-пароль", "новый-пароль")

    assert verify_password("pw", head.password_hash)


def test_mismatched_new_passwords_are_refused(db, head):
    with pytest.raises(AccountError, match="не совпали"):
        accounts.change_own_password(db, head, "pw", "первый-вариант", "второй-вариант")


def test_short_new_password_is_refused(db, head):
    with pytest.raises(AccountError, match="короче"):
        accounts.change_own_password(db, head, "pw", "123", "123")


def test_setting_a_password_burns_a_pending_invite(db, head):
    head.invite_code = "старая-ссылка"
    db.commit()

    accounts.change_own_password(db, head, "pw", "новый-пароль", "новый-пароль")

    assert head.invite_code is None


# --- удаление -------------------------------------------------------------

def test_deleting_a_person_takes_their_data_with_them(db, head, member):
    nutrition.create_draft(db, member.id, MealEstimate("Овсянка", 320, 12, 14, 34))
    memory.add_note(db, member.id, "личная заметка")
    member_id = member.id

    accounts.delete_member(db, head, member_id)

    assert db.get(User, member_id) is None
    assert nutrition.meals_for_day(db, member_id) == []
    assert memory.list_notes(db, member_id) == []


def test_you_cannot_delete_yourself(db, head):
    with pytest.raises(AccountError, match="Себя"):
        accounts.delete_member(db, head, head.id)


def test_a_member_cannot_delete_anyone(db, head, member):
    with pytest.raises(AccountError, match="глава семьи"):
        accounts.delete_member(db, member, head.id)


def test_one_head_can_delete_another(db, head, member):
    accounts.set_head(db, head, member.id, True)
    other_id = member.id

    accounts.delete_member(db, head, other_id)

    assert db.get(User, other_id) is None


def test_people_from_another_family_are_invisible(db, head):
    from app.core.models import Family

    other_family = Family(name="Соседи")
    db.add(other_family)
    db.flush()
    stranger = User(family_id=other_family.id, username="stranger", display_name="Сосед")
    db.add(stranger)
    db.commit()

    with pytest.raises(AccountError, match="нет в вашей семье"):
        accounts.delete_member(db, head, stranger.id)
    with pytest.raises(AccountError, match="нет в вашей семье"):
        accounts.issue_invite(db, head, stranger.id)


# --- сводка для экрана ----------------------------------------------------

def test_overview_shows_how_each_person_gets_in(db, head, member):
    member.invite_code = "код"
    db.commit()

    rows = {row["user"].id: row for row in accounts.overview(db, head)}

    assert rows[head.id]["status"] == "заходит сам"
    assert rows[member.id]["status"] == "ждёт приглашения"
    assert rows[head.id]["can_delete"] is False           # себя не удалить
    assert rows[head.id]["can_change_role"] is False      # и роль себе не сменить
    assert rows[member.id]["can_delete"] is True


def test_a_member_sees_no_management_buttons(db, head, member):
    rows = {row["user"].id: row for row in accounts.overview(db, member)}

    assert all(not row["can_delete"] and not row["can_change_role"] for row in rows.values())
