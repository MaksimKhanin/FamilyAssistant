"""Модуль «Подход»: разбор разговора и то, куда ложится его выжимка."""
from app.agent.runtime import save_message
from app.core import instructions
from app.modules.relationship import service
from tests.conftest import FakeLLM


def _talk(db, member, lines=4):
    for i in range(lines):
        save_message(db, member, "user", f"реплика {i}")
        save_message(db, member, "assistant", f"ответ {i}")


def test_the_digest_does_not_overwrite_the_handwritten_memo(db, member):
    """Тикет #75: авто-выжимка живёт под своим ключом, памятка человека цела."""
    instructions.set_memo(db, member.id, service.MODULE, "со мной лучше коротко")
    _talk(db, member)

    assert service.run_review(db, member, llm=FakeLLM([{
        "add": ["любит утренние сводки"], "merge": [], "remove": [],
        "summary": "Говорили про сводки.",
        "digest": "Коротко и по делу; любит утренние сводки.",
    }]))

    assert instructions.memo(db, member.id, service.MODULE) == "со мной лучше коротко"
    assert instructions.memo(db, member.id, instructions.auto_key(service.MODULE)) == (
        "Коротко и по делу; любит утренние сводки.")


def test_both_memo_and_digest_ride_into_the_prompt(db, member):
    instructions.set_memo(db, member.id, service.MODULE, "со мной лучше коротко")
    instructions.set_memo(db, member.id, instructions.auto_key(service.MODULE), "любит сводки")

    pairs = instructions.for_prompt(db, member.id, ["relationship"])

    titles = [title for title, _ in pairs]
    assert any("заметки ассистента" in title for title in titles)
    texts = [text for _, text in pairs]
    assert "со мной лучше коротко" in texts and "любит сводки" in texts


def test_notes_and_summary_land_on_their_boards(db, member):
    from app.modules.memory import knowledge

    _talk(db, member)
    service.run_review(db, member, llm=FakeLLM([{
        "add": ["не любит списки"], "merge": [], "remove": [],
        "summary": "Короткий разговор.", "digest": "",
    }]))

    notes = knowledge.approach_notes_board(db, member.id, create=False)
    entries = knowledge.list_entries(db, member.id, notes.id)
    assert [e.text for e in entries] == ["не любит списки"]
    summaries = knowledge.approach_summaries_board(db, member.id, create=False)
    assert [e.text for e in knowledge.list_entries(db, member.id, summaries.id)] == [
        "Короткий разговор."]


# --- «Подход» по умолчанию у новых участников (ADR-0015, тикет #76) ---------

def test_a_fresh_member_gets_reviews_by_default(db, member, monkeypatch):
    """Нового человека (без строки module_access) планировщик разбирает сам."""
    from app import scheduler

    reviewed = []
    monkeypatch.setattr(service, "due", lambda _db, user_id: True)
    monkeypatch.setattr(service, "run_review", lambda _db, user, llm=None: reviewed.append(user.id))

    scheduler.run_relationship_reviews(db)

    assert member.id in reviewed


def test_an_explicit_opt_out_still_holds(db, member, monkeypatch):
    """Явное «выключено» (строки миграций 0015/0016) default не перебивает."""
    from app import scheduler
    from app.core.access import set_module_enabled

    set_module_enabled(db, member.id, "relationship", False)
    reviewed = []
    monkeypatch.setattr(service, "due", lambda _db, user_id: True)
    monkeypatch.setattr(service, "run_review", lambda _db, user, llm=None: reviewed.append(user.id))

    scheduler.run_relationship_reviews(db)

    assert member.id not in reviewed


# --- память о прошлых разговорах (тикет #77) --------------------------------

def test_past_summaries_ride_into_the_system_prompt(db, member):
    from app.agent.llm import LLMResponse
    from app.agent.runtime import Agent
    from app.modules.memory import knowledge

    board = knowledge.approach_summaries_board(db, member.id, create=True)
    knowledge.add_assistant_entry(db, member.id, board.id, "Обсуждали отпуск в горах.")

    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, member, "привет")

    system = llm.calls[0]["messages"][0]["content"]
    assert "О чём вы говорили раньше" in system
    assert "Обсуждали отпуск в горах." in system


def test_without_summaries_the_prompt_stays_clean(db, member):
    from app.agent.llm import LLMResponse
    from app.agent.runtime import Agent

    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, member, "привет")

    assert "О чём вы говорили раньше" not in llm.calls[0]["messages"][0]["content"]


def test_the_morning_digest_offers_a_followup_topic(db, member, monkeypatch):
    from app import scheduler
    from app.agent import voice
    from app.core.models import ScheduledJob
    from app.modules.memory import knowledge
    from datetime import datetime

    board = knowledge.approach_summaries_board(db, member.id, create=True)
    knowledge.add_assistant_entry(db, member.id, board.id, "Обсуждали отпуск в горах.")

    heard = {}

    def fake_speak(subject, hint, fallback="", llm=None):
        heard["hint"] = hint
        return fallback

    monkeypatch.setattr(voice, "speak", fake_speak)
    db.add(ScheduledJob(user_id=member.id, kind="morning_digest", at_time="08:00", enabled=True))
    db.commit()

    scheduler.run_jobs(db, datetime(2026, 8, 9, 8, 0))

    assert "Обсуждали отпуск в горах." in heard.get("hint", "")
    assert "вернись к этой теме" in heard["hint"]
