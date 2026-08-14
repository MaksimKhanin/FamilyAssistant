"""Администратор из окружения: заводится один раз и больше не мешает."""
import pytest

from app.core.auth import hash_password, verify_password
from app.core.bootstrap import ensure_admin
from app.core.models import ROLE_ADMIN, Family, User


@pytest.fixture
def admin_env(monkeypatch):
    """Заполненные ADMIN_* — как в .env при развёртывании."""
    admin = pytest.importorskip("app.core.config").settings.admin
    monkeypatch.setattr(admin, "username", "admin")
    monkeypatch.setattr(admin, "password", "первый-пароль")
    monkeypatch.setattr(admin, "display_name", "Администратор")
    monkeypatch.setattr(admin, "relation", "")
    monkeypatch.setattr(admin, "family_name", "Наша семья")
    monkeypatch.setattr(admin, "reset_password", False)
    return admin


def test_empty_database_gets_a_family_and_an_administrator(db, admin_env):
    created = ensure_admin(db)

    assert created is not None
    assert created.role == ROLE_ADMIN
    assert created.display_name == "Администратор"
    assert verify_password("первый-пароль", created.password_hash)
    assert db.query(Family).one().name == "Наша семья"


def test_running_twice_changes_nothing(db, admin_env):
    first = ensure_admin(db)
    first.display_name = "Главный админ"
    db.commit()

    ensure_admin(db)

    assert db.query(User).count() == 1
    assert db.query(User).one().display_name == "Главный админ"


def test_password_changed_in_the_panel_survives_a_restart(db, admin_env):
    """Иначе рестарт контейнера откатывал бы пароль к значению из .env."""
    ensure_admin(db)
    admin = db.query(User).one()
    admin.password_hash = hash_password("новый-пароль-из-панели")
    db.commit()

    ensure_admin(db)

    assert verify_password("новый-пароль-из-панели", db.query(User).one().password_hash)


def test_reset_flag_brings_the_password_back(db, admin_env, monkeypatch):
    ensure_admin(db)
    admin = db.query(User).one()
    admin.password_hash = hash_password("забытый-пароль")
    db.commit()

    monkeypatch.setattr(admin_env, "reset_password", True)
    ensure_admin(db)

    assert verify_password("первый-пароль", db.query(User).one().password_hash)


def test_reset_also_restores_the_admin_role(db, admin_env, monkeypatch):
    ensure_admin(db)
    admin = db.query(User).one()
    admin.role = "other"
    db.commit()

    monkeypatch.setattr(admin_env, "reset_password", True)
    ensure_admin(db)

    assert db.query(User).one().role == ROLE_ADMIN


def test_existing_family_is_left_alone(db, admin_env, member):
    """Люди уже есть, просто под другими логинами — не вмешиваемся."""
    assert ensure_admin(db) is None
    assert db.query(User).count() == 1


def test_without_a_password_nothing_is_created(db, admin_env, monkeypatch):
    monkeypatch.setattr(admin_env, "password", "")

    assert ensure_admin(db) is None
    assert db.query(User).count() == 0
