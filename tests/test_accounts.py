"""Учётные записи: кто кого может завести, разжаловать и удалить.

Роли две и они не пересекаются (ADR-0007): администратор ведёт учётки и не
пользуется ассистентом, участник пользуется и учёток не трогает.
"""
import pytest

from app.core import accounts
from app.core.accounts import AccountError
from app.core.auth import hash_password, verify_password
from app.core.models import ROLE_ADMIN, ROLE_MEMBER, User
from app.modules.memory import knowledge
from app.modules.nutrition import service as nutrition
from app.modules.nutrition.vision import MealEstimate


# --- создание -------------------------------------------------------------

def test_admin_adds_a_member_who_will_set_their_own_password(db, admin):
    member = accounts.create_member(db, admin, "Соня", "дочь")

    assert member.username == "sonya"
    assert member.role == ROLE_MEMBER
    assert member.invite_code                 # ссылка выдана
    assert member.password_hash is None       # пароль придумает сам


def test_member_cannot_add_anyone(db, member):
    with pytest.raises(AccountError, match="администратор"):
        accounts.create_member(db, member, "Кто-то")


def test_login_is_derived_from_the_name_and_never_collides(db, admin):
    first = accounts.create_member(db, admin, "Лёва")
    second = accounts.create_member(db, admin, "Лёва")

    assert first.username == "leva"
    assert second.username == "leva2"


def test_nameless_member_is_refused(db, admin):
    with pytest.raises(AccountError, match="имени"):
        accounts.create_member(db, admin, "   ")


def test_family_size_is_bounded(db, admin, monkeypatch):
    """Лимит считает людей: админская учётка — вход в панель, а не человек за столом."""
    monkeypatch.setattr(accounts, "MAX_MEMBERS", 2)
    accounts.create_member(db, admin, "Соня")
    accounts.create_member(db, admin, "Лёва")

    with pytest.raises(AccountError, match="не семья"):
        accounts.create_member(db, admin, "Третий")


# --- роли -----------------------------------------------------------------

def test_admin_can_promote_someone(db, admin, member):
    accounts.set_admin(db, admin, member.id, True)
    assert member.role == ROLE_ADMIN


def test_nobody_changes_their_own_role(db, admin):
    """Единственный админ не должен уметь запереть себя снаружи."""
    with pytest.raises(AccountError, match="Свою роль"):
        accounts.set_admin(db, admin, admin.id, False)


def test_a_second_admin_can_demote_the_first(db, admin, member):
    accounts.set_admin(db, admin, member.id, True)
    accounts.set_admin(db, member, admin.id, False)

    assert admin.role == ROLE_MEMBER
    assert member.role == ROLE_ADMIN


# --- приглашения ----------------------------------------------------------

def test_reissuing_the_link_invalidates_the_old_one_and_the_password(db, admin, member):
    member.password_hash = hash_password("старый-пароль")
    member.invite_code = "старый-код"
    db.commit()

    accounts.issue_invite(db, admin, member.id)

    assert member.invite_code != "старый-код"
    assert member.password_hash is None
    assert not verify_password("старый-пароль", member.password_hash)


def test_revoking_a_link_leaves_an_existing_login_alone(db, admin, member):
    member.password_hash = hash_password("рабочий-пароль")
    member.invite_code = "код"
    db.commit()

    accounts.revoke_invite(db, admin, member.id)

    assert member.invite_code is None
    assert verify_password("рабочий-пароль", member.password_hash)


def test_member_cannot_reset_anyones_password(db, member, admin):
    with pytest.raises(AccountError):
        accounts.issue_invite(db, member, admin.id)


def test_admin_cannot_reset_their_own_password_this_way(db, admin):
    """Иначе единственный админ стирал бы себе пароль и оставался снаружи."""
    with pytest.raises(AccountError, match="в профиле"):
        accounts.issue_invite(db, admin, admin.id)

    assert admin.password_hash is not None


# --- свой пароль ----------------------------------------------------------

def test_changing_own_password(db, admin):
    accounts.change_own_password(db, admin, "pw", "новый-пароль", "новый-пароль")

    assert verify_password("новый-пароль", admin.password_hash)
    assert not verify_password("pw", admin.password_hash)


def test_wrong_current_password_changes_nothing(db, admin):
    with pytest.raises(AccountError, match="Текущий пароль"):
        accounts.change_own_password(db, admin, "не тот", "новый-пароль", "новый-пароль")

    assert verify_password("pw", admin.password_hash)


def test_mismatched_new_passwords_are_refused(db, admin):
    with pytest.raises(AccountError, match="не совпали"):
        accounts.change_own_password(db, admin, "pw", "первый-вариант", "второй-вариант")


def test_short_new_password_is_refused(db, admin):
    with pytest.raises(AccountError, match="короче"):
        accounts.change_own_password(db, admin, "pw", "123", "123")


def test_setting_a_password_burns_a_pending_invite(db, admin):
    admin.invite_code = "старая-ссылка"
    db.commit()

    accounts.change_own_password(db, admin, "pw", "новый-пароль", "новый-пароль")

    assert admin.invite_code is None


# --- удаление -------------------------------------------------------------

def test_deleting_a_person_takes_their_data_with_them(db, admin, member):
    nutrition.create_draft(db, member.id, MealEstimate("Овсянка", 320, 12, 14, 34))
    section = knowledge.create_section(db, member.id, "Личное")
    knowledge.create_board(db, member.id, section.id, "Наблюдения")
    member_id = member.id

    accounts.delete_member(db, admin, member_id)

    assert db.get(User, member_id) is None
    assert nutrition.meals_for_day(db, member_id) == []
    assert knowledge.list_sections(db, member_id) == []


def test_you_cannot_delete_yourself(db, admin):
    with pytest.raises(AccountError, match="Себя"):
        accounts.delete_member(db, admin, admin.id)


def test_a_member_cannot_delete_anyone(db, admin, member):
    with pytest.raises(AccountError, match="администратор"):
        accounts.delete_member(db, member, admin.id)



def test_one_admin_can_delete_another(db, admin, member):
    accounts.set_admin(db, admin, member.id, True)
    other_id = member.id

    accounts.delete_member(db, admin, other_id)

    assert db.get(User, other_id) is None


def test_people_from_another_family_are_invisible(db, admin):
    from app.core.models import Family

    other_family = Family(name="Соседи")
    db.add(other_family)
    db.flush()
    stranger = User(family_id=other_family.id, username="stranger", display_name="Сосед")
    db.add(stranger)
    db.commit()

    with pytest.raises(AccountError, match="нет в вашей семье"):
        accounts.delete_member(db, admin, stranger.id)
    with pytest.raises(AccountError, match="нет в вашей семье"):
        accounts.issue_invite(db, admin, stranger.id)


# --- сводка для экрана ----------------------------------------------------

def test_overview_shows_how_each_person_gets_in(db, admin, member):
    member.password_hash = None
    member.invite_code = "код"
    db.commit()

    rows = {row["user"].id: row for row in accounts.overview(db, admin)}

    assert rows[admin.id]["status"] == "заходит сам"
    assert rows[member.id]["status"] == "ждёт приглашения"
    assert rows[admin.id]["can_delete"] is False           # себя не удалить
    assert rows[admin.id]["can_change_role"] is False      # и роль себе не сменить
    assert rows[member.id]["can_delete"] is True


def test_a_member_sees_no_management_buttons(db, admin, member):
    rows = {row["user"].id: row for row in accounts.overview(db, member)}

    assert all(not row["can_delete"] and not row["can_change_role"] for row in rows.values())


def test_becoming_an_administrator_keeps_the_records(db, admin, member):
    """Смена роли — про доступ, а не про данные: записи человека остаются на месте."""
    section = knowledge.create_section(db, member.id, "Личное")
    knowledge.create_board(db, member.id, section.id, "Наблюдения")

    accounts.set_admin(db, admin, member.id, True)

    assert member.is_admin
    assert [s.name for s in knowledge.list_sections(db, member.id)] == ["Личное"]
