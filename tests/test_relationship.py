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
