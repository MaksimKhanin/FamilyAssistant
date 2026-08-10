"""Трейсы агента: что записывается, как складываются токены и кто это видит."""
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import tracing
from app.agent.llm import LLMClient, LLMResponse, ToolCall
from app.agent.runtime import Agent
from app.core.auth import hash_password
from app.core.db import get_db
from app.core.models import AgentRun, AgentTraceStep
from app.main import app
from tests.conftest import FakeLLM


def _call(name, **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)])


def _answer(db, user, text="запомни: Соня не ест грибы"):
    llm = FakeLLM([_call("remember", text="Соня не ест грибы", kind="pref"),
                   LLMResponse(content="Запомнил.")])
    return Agent(llm).respond(db, user, text)


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_head(client, head):
    client.post("/login", data={"username": head.username, "password": "pw"}, follow_redirects=False)
    return client


# --- запись ---------------------------------------------------------------

def test_a_reply_becomes_a_run_with_the_tool_call_in_it(db, head):
    head.autonomy = 3
    db.commit()

    _answer(db, head)

    run = db.query(AgentRun).one()
    assert run.user_id == head.id
    assert run.trigger.startswith("запомни")
    assert run.reply == "Запомнил."
    assert run.duration_ms >= 1

    step = db.query(AgentTraceStep).one()
    assert (step.kind, step.name, step.status) == ("tool", "remember", "ok")
    assert "Соня не ест грибы" in step.request_json
    assert "summary" in step.response_json


def test_nothing_is_written_while_the_switch_is_off(db, head):
    tracing.get_settings(db, head.family_id).enabled = False
    db.commit()

    _answer(db, head)

    assert db.query(AgentRun).count() == 0
    assert db.query(AgentTraceStep).count() == 0


def test_the_exact_request_to_the_model_is_kept(db, head, monkeypatch):
    """Ради этого экран и заводился: видно, что модель получила на самом деле."""
    answer = {"choices": [{"message": {"content": "Здравствуйте."}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 31, "completion_tokens": 3, "total_tokens": 34}}
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None:
                        httpx.Response(200, json=answer, request=httpx.Request("POST", url)))

    with tracing.run(db, head, head, "web", "привет"):
        LLMClient().chat([{"role": "system", "content": "ты ассистент"},
                          {"role": "user", "content": "привет"}])

    step = db.query(AgentTraceStep).one()
    assert step.kind == "llm"
    assert "ты ассистент" in step.request_json          # системный промпт целиком
    assert "reasoning_effort" in step.request_json      # и обёртки вокруг него
    assert (step.prompt_tokens, step.completion_tokens, step.total_tokens) == (31, 3, 34)

    run = db.query(AgentRun).one()
    assert (run.total_tokens, run.llm_calls) == (34, 1)


def test_a_huge_value_is_trimmed_instead_of_bloating_the_base(db, head):
    """Фото в base64 — мегабайты; в трейсе от него нужен только факт."""
    with tracing.run(db, head, head, "web", "фото") as recorder:
        recorder.tool("estimate_meal", {"image": "x" * 100_000}, {"ok": True})

    step = db.query(AgentTraceStep).one()
    assert len(step.request_json) < tracing.VALUE_LIMIT + 500
    assert "обрезано" in step.request_json


# --- сессии и сводки ------------------------------------------------------

def test_replies_in_a_row_land_in_one_conversation(db, head):
    head.autonomy = 3
    db.commit()

    _answer(db, head)
    _answer(db, head, "и ещё запомни")

    sessions = {run.session_id for run in db.query(AgentRun).all()}
    assert len(sessions) == 1


def test_a_long_pause_starts_a_new_conversation(db, head):
    head.autonomy = 3
    db.commit()

    _answer(db, head)
    old = db.query(AgentRun).one()
    old.created_at = datetime.utcnow() - tracing.SESSION_GAP - timedelta(minutes=1)
    db.commit()

    _answer(db, head, "спустя час")

    sessions = {run.session_id for run in db.query(AgentRun).all()}
    assert len(sessions) == 2


def test_tokens_are_summed_per_person(db, head, member):
    with tracing.run(db, head, head, "web", "раз") as recorder:
        recorder.llm({"model": "m"}, {}, usage={"prompt_tokens": 10, "completion_tokens": 5,
                                                "total_tokens": 15})
    with tracing.run(db, member, member, "telegram", "два") as recorder:
        recorder.llm({"model": "m"}, {}, usage={"prompt_tokens": 1, "completion_tokens": 1,
                                                "total_tokens": 2})

    by_user = {row["name"]: row for row in tracing.by_user(db, head.family_id)}
    assert by_user[head.display_name]["total_tokens"] == 15
    assert by_user[member.display_name]["total_tokens"] == 2
    assert by_user[head.display_name]["sessions"] == 1


def test_old_runs_are_forgotten(db, head):
    tracing.get_settings(db, head.family_id).keep_runs = 2
    db.commit()

    for number in range(4):
        with tracing.run(db, head, head, "web", f"реплика {number}"):
            pass

    triggers = [row.trigger for row in db.query(AgentRun).order_by(AgentRun.id).all()]
    assert triggers == ["реплика 2", "реплика 3"]


# --- экран и выгрузка -----------------------------------------------------

def test_the_screen_is_closed_to_everyone_but_the_head(client, db, member):
    member.password_hash = hash_password("pw")
    db.commit()
    client.post("/login", data={"username": member.username, "password": "pw"}, follow_redirects=False)

    response = client.get("/settings/traces")

    assert response.status_code == 200
    assert "только для главы семьи" in response.text
    assert "Токены по людям" not in response.text


def test_the_head_sees_the_run_on_the_screen(as_head, db, head):
    head.autonomy = 3
    db.commit()
    _answer(db, head)

    response = as_head.get("/settings/traces")

    assert response.status_code == 200
    assert "Токены по людям" in response.text
    assert "remember" in response.text


def test_export_returns_a_json_attachment_with_the_prompts(as_head, db, head):
    head.autonomy = 3
    db.commit()
    _answer(db, head)

    response = as_head.get("/settings/traces/export.json")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    data = response.json()
    assert data["runs"][0]["reply"] == "Запомнил."
    assert data["runs"][0]["steps"][0]["name"] == "remember"
    assert data["by_user"][0]["total_tokens"] == 0        # FakeLLM токенов не отдаёт


def test_export_can_be_narrowed_to_one_conversation(as_head, db, head):
    head.autonomy = 3
    db.commit()
    _answer(db, head)
    session_id = db.query(AgentRun).one().session_id

    response = as_head.get(f"/settings/traces/export.json?session_id={session_id}")

    assert [run["session_id"] for run in response.json()["runs"]] == [session_id]


def test_clearing_wipes_the_prompts(as_head, db, head):
    head.autonomy = 3
    db.commit()
    _answer(db, head)

    as_head.post("/settings/traces/clear", follow_redirects=False)

    assert db.query(AgentRun).count() == 0
    assert db.query(AgentTraceStep).count() == 0
