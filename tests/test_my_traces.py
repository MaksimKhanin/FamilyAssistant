"""«Мои трейсы» — участнический экран: только свои прогоны, для отладки."""
import pytest
from fastapi.testclient import TestClient

from app.agent import policy, tracing
from app.agent.runtime import Agent
from app.core.db import get_db
from app.core.models import AgentRun
from app.main import app
from tests.conftest import FakeLLM


def _call(name, **arguments):
    from app.agent.llm import LLMResponse, ToolCall
    return LLMResponse(tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)])


def _answer(db, user, text="запомни: Соня не ест грибы"):
    from app.agent.llm import LLMResponse
    llm = FakeLLM([_call("remember", text="Соня не ест грибы", kind="pref"),
                   LLMResponse(content="Запомнил.")])
    return Agent(llm).respond(db, user, text)


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _login(client, user):
    client.post("/login", data={"username": user.username, "password": "pw"}, follow_redirects=False)
    return client


def test_a_member_sees_their_own_screen(client, db, member):
    policy.set_autonomy(db, member.family_id, 3)
    _answer(db, member)
    _login(client, member)

    response = client.get("/settings/my-traces")

    assert response.status_code == 200
    assert "remember" in response.text


def test_the_screen_is_closed_to_the_administrator(client, db, admin):
    """Админская учётка не разговаривает с ассистентом — своих прогонов у неё нет."""
    _login(client, admin)

    response = client.get("/settings/my-traces")

    assert response.status_code == 403


def test_a_member_does_not_see_someone_elses_runs(client, db, member, other):
    policy.set_autonomy(db, member.family_id, 3)
    _answer(db, member)
    _answer(db, other, "и второй разговор")
    _login(client, member)

    response = client.get("/settings/my-traces")

    assert response.status_code == 200
    assert "и второй разговор" not in response.text


def test_export_only_contains_the_callers_own_runs(client, db, member, other):
    policy.set_autonomy(db, member.family_id, 3)
    _answer(db, member)
    _answer(db, other, "чужой разговор")
    _login(client, member)

    response = client.get("/settings/my-traces/export.json")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    data = response.json()
    assert len(data["runs"]) == 1
    assert data["runs"][0]["user"]["id"] == member.id


def test_export_by_run_id_cannot_reach_someone_elses_run(client, db, member, other):
    """`run_id` в адресе не должен открывать чужой прогон при подмене числа."""
    policy.set_autonomy(db, member.family_id, 3)
    _answer(db, other, "чужой разговор")
    foreign_run_id = db.query(AgentRun).one().id
    _login(client, member)

    response = client.get(f"/settings/my-traces/export.json?run_id={foreign_run_id}")

    assert response.status_code == 200
    assert response.json()["runs"] == []


def test_a_session_filter_still_stays_within_ones_own_runs(client, db, member):
    policy.set_autonomy(db, member.family_id, 3)
    _answer(db, member)
    session_id = db.query(AgentRun).one().session_id
    _login(client, member)

    response = client.get(f"/settings/my-traces?session_id={session_id}")

    assert response.status_code == 200
    assert "remember" in response.text
