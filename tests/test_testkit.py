"""Стенд: служебные ручки, которыми сценарии проходят панель снаружи.

Проверяется здесь не панель, а сам стенд — иначе он врёт о ней. Три вещи, из-за
которых прогон сценария стал бы бессмысленным: ручки открываются без ключа;
`say` отвечает, но не показывает, чем ответ получился; режиссёр модели не
подменяет модель. Ну и главное, чего от стенда ждут в бою: без ключа его нет.
"""
import pytest
from fastapi.testclient import TestClient

from app.agent import policy
from app.core.config import settings
from app.core.db import get_db
from app.main import app
from app.testkit import director, journal

TOKEN = settings.testkit_token
HEAD = {"X-Testkit-Token": TOKEN}


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()
    director.set_script()
    director.forget_calls()
    journal.clear()


def test_the_stand_is_closed_without_the_key(client):
    assert client.get("/api/testkit/health").status_code == 401
    assert client.get("/api/testkit/health", headers={"X-Testkit-Token": "wrong-key"}).status_code == 401


def test_a_short_key_does_not_switch_the_stand_on(monkeypatch):
    """Ключ покороче — то же, что его отсутствие: стенд ходит мимо входа и ролей."""
    from app import testkit

    monkeypatch.setattr(settings, "testkit_token", "коротко")
    assert testkit.enabled() is False


def test_it_says_who_lives_here(client, member):
    answer = client.get("/api/testkit/health", headers=HEAD).json()

    assert answer["ok"] is True
    assert "marina" in [user["username"] for user in answer["users"]]


def test_a_word_to_the_assistant_comes_back_with_its_insides(client, db, member):
    """`say` отдаёт не только реплику, но и то, чем она получилась."""
    policy.set_autonomy(db, member.family_id, 3)
    client.post("/api/testkit/model/script", headers=HEAD, json={
        "chat": [{"tool": "remember", "arguments": {"text": "Соня не ест грибы"}},
                 {"content": "Запомнила."}],
    })

    answer = client.post("/api/testkit/say", headers=HEAD,
                         json={"user": "marina", "text": "запомни: Соня не ест грибы"}).json()

    assert answer["reply"] == "Запомнила."
    assert [trace["tool"] for trace in answer["traces"]] == ["remember"]
    assert answer["error"] is None
    # Обращения к модели видны даже без трейсов: запись прогонов — отдельная настройка.
    assert answer["model_calls"][0]["scripted"] is True


def test_the_director_can_break_the_model_on_purpose(client, db, member):
    """Модель, которая не отвечает, — это не падение разговора, а честный ответ."""
    client.post("/api/testkit/model/script", headers=HEAD,
                json={"chat": [{"error": "Модель недоступна"}]})

    answer = client.post("/api/testkit/say", headers=HEAD,
                         json={"user": "marina", "text": "съел суп"}).json()

    assert "не могу подумать" in answer["reply"]
    assert answer["traces"] == []


def test_a_prepared_action_waits_and_then_runs(client, db, member):
    policy.set_autonomy(db, member.family_id, 0)
    client.post("/api/testkit/model/script", headers=HEAD, json={
        "chat": [{"tool": "remember", "arguments": {"text": "у Лёвы аллергия на орехи"}},
                 {"content": "Подготовила."}],
    })

    said = client.post("/api/testkit/say", headers=HEAD,
                       json={"user": "marina", "text": "запомни про аллергию"}).json()
    assert len(said["pending"]) == 1

    done = client.post("/api/testkit/confirm", headers=HEAD,
                       json={"user": "marina", "decision": "approve"}).json()

    assert done["ok"] is True
    assert done["pending"] == []


def test_the_snapshot_shows_a_persons_own_rows(client, db, member, other):
    from app.modules.nutrition.models import Meal

    db.add(Meal(user_id=member.id, title="борщ", kcal=400))
    db.add(Meal(user_id=other.id, title="каша", kcal=300))
    db.commit()

    mine = client.get("/api/testkit/state", headers=HEAD,
                      params={"user": "marina", "tables": "meals"}).json()

    titles = [row["title"] for row in mine["tables"]["meals"]]
    assert titles == ["борщ"]


def test_the_journal_remembers_requests_with_their_warnings(client, member):
    """Ради этого журнал и заведён: предупреждение сервера видно снаружи."""
    since = client.get("/api/testkit/cursor", headers=HEAD).json()["cursor"]
    client.get("/login")

    rows = client.get("/api/testkit/requests", headers=HEAD,
                      params={"since": since}).json()["requests"]

    assert [row["path"] for row in rows] == ["/login"]
    assert rows[0]["status"] == 200


def test_the_stand_does_not_write_itself_into_the_journal(client, member):
    since = client.get("/api/testkit/cursor", headers=HEAD).json()["cursor"]
    client.get("/api/testkit/health", headers=HEAD)

    rows = client.get("/api/testkit/requests", headers=HEAD,
                      params={"since": since}).json()["requests"]

    assert rows == []


def test_the_stand_is_not_advertised_in_the_schema(client):
    """Ручки стенда не перечисляются в `/openapi.json`: он открыт кому угодно."""
    paths = client.get("/openapi.json").json()["paths"]

    assert not [path for path in paths if path.startswith("/api/testkit")]


def test_wiping_the_base_asks_for_a_word(client, member):
    assert client.post("/api/testkit/reset", headers=HEAD, json={}).status_code == 400
