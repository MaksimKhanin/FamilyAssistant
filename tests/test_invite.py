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
def invited(db, other):
    other.invite_code = new_invite_code()
    db.commit()
    return other


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


def test_new_member_gets_an_invite_link(client, db, admin):
    """Людей заводит администратор — с онбординга или с «Учётных записей»."""
    client.post("/login", data={"username": admin.username, "password": "pw"}, follow_redirects=False)

    client.post("/onboarding/member", data={"display_name": "Соня", "relation": "дочь"},
                follow_redirects=False)

    sonya = db.query(User).filter(User.display_name == "Соня").one()
    assert sonya.invite_code
    assert sonya.password_hash is None
    assert sonya.username == "sonya"


# --- адрес в ссылке -------------------------------------------------------------

def test_the_link_points_at_the_address_the_admin_is_using(client, db, admin, monkeypatch):
    """Ссылка собирается от адреса запроса, а не от умолчания «localhost».

    Панель дома открывают по адресу вроде http://192.168.1.50:5000 — по нему же
    пойдёт и приглашённый. С незаданным PUBLIC_BASE_URL ссылка собиралась без
    единой ошибки и не открывалась ни с одного другого устройства.
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "public_base_url_explicit", False)
    client.post("/login", data={"username": admin.username, "password": "pw"}, follow_redirects=False)
    client.post("/settings/accounts/member", data={"display_name": "Соня"}, follow_redirects=False)

    markup = client.get("/settings/accounts", headers={"host": "192.168.1.50:5000"}).text

    sonya = db.query(User).filter(User.display_name == "Соня").one()
    assert f"http://192.168.1.50:5000/invite/{sonya.invite_code}" in markup


def test_an_explicit_public_base_url_still_wins(client, db, admin, monkeypatch):
    """Панель за доменом знает о себе больше, чем запрос изнутри контура."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "public_base_url", "https://dom.example.com")
    monkeypatch.setattr(settings, "public_base_url_explicit", True)
    client.post("/login", data={"username": admin.username, "password": "pw"}, follow_redirects=False)
    client.post("/settings/accounts/member", data={"display_name": "Соня"}, follow_redirects=False)

    markup = client.get("/settings/accounts").text

    sonya = db.query(User).filter(User.display_name == "Соня").one()
    assert f"https://dom.example.com/invite/{sonya.invite_code}" in markup


# --- ссылка на экране «Учётные записи» -------------------------------------------

def test_the_link_waits_in_the_card_until_it_is_used(client, db, admin):
    """Раньше ссылка показывалась ровно один раз, и, уйдя с экрана, найти её было
    негде — оставалось выпускать новую и объяснять человеку, почему первая
    перестала работать."""
    client.post("/login", data={"username": admin.username, "password": "pw"}, follow_redirects=False)
    client.post("/settings/accounts/member", data={"display_name": "Соня"}, follow_redirects=False)
    sonya = db.query(User).filter(User.display_name == "Соня").one()
    code = sonya.invite_code

    # Экран открыт заново, без «сразу после выпуска» в адресе.
    assert f"/invite/{code}" in client.get("/settings/accounts").text

    client.post(f"/invite/{code}", data={"password": "хороший-пароль",
                                         "password_repeat": "хороший-пароль"},
                follow_redirects=False)

    # Человек вошёл — показывать больше нечего.
    admin_view = TestClient(app)
    admin_view.post("/login", data={"username": admin.username, "password": "pw"},
                    follow_redirects=False)
    markup = admin_view.get("/settings/accounts").text
    assert f"/invite/{code}" not in markup
    assert "заходит сам" in markup
