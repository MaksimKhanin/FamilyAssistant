"""Приглашение по ссылке — единственный способ, которым участник заводит себе вход."""
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.core.models import User
from app.main import app
from app.web.routes_invite import new_invite_code


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def invited(db, member):
    member.invite_code = new_invite_code()
    db.commit()
    return member


def test_invite_page_greets_the_person_by_name(client, invited):
    response = client.get(f"/invite/{invited.invite_code}")
    assert response.status_code == 200
    assert invited.display_name in response.text
    assert invited.username in response.text


def test_setting_a_password_logs_the_person_in(client, db, invited):
    code = invited.invite_code
    response = client.post(f"/invite/{code}",
                           data={"password": "хороший-пароль", "password_repeat": "хороший-пароль"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "family_session" in response.cookies

    db.refresh(invited)
    assert invited.password_hash
    assert invited.invite_code is None       # ссылка сгорела


def test_the_link_works_only_once(client, db, invited):
    code = invited.invite_code
    client.post(f"/invite/{code}", data={"password": "первый-пароль", "password_repeat": "первый-пароль"},
                follow_redirects=False)

    assert client.get(f"/invite/{code}").status_code == 404


def test_mismatched_passwords_do_not_create_an_account(client, db, invited):
    response = client.post(f"/invite/{invited.invite_code}",
                           data={"password": "первый", "password_repeat": "второй"})

    assert response.status_code == 400
    assert "не совпали" in response.text
    db.refresh(invited)
    assert invited.password_hash is None


def test_short_password_is_refused(client, db, invited):
    response = client.post(f"/invite/{invited.invite_code}",
                           data={"password": "123", "password_repeat": "123"})

    assert response.status_code == 400
    db.refresh(invited)
    assert invited.password_hash is None


def test_unknown_code_says_so_plainly(client):
    response = client.get("/invite/этого-кода-нет")
    assert response.status_code == 404
    assert "уже не работает" in response.text


def test_new_member_gets_an_invite_link(client, db, head):
    from app.core.auth import hash_password

    head.password_hash = hash_password("pw")
    db.commit()
    client.post("/login", data={"username": head.username, "password": "pw"}, follow_redirects=False)

    client.post("/onboarding/member", data={"display_name": "Соня", "relation": "дочь"},
                follow_redirects=False)

    sonya = db.query(User).filter(User.display_name == "Соня").one()
    assert sonya.invite_code
    assert sonya.password_hash is None
    assert sonya.username == "sonya"
