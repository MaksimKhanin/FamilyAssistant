"""Инструменты агента для досок: проекция полномочий человека на ассистента.

Ассистент видит ровно то же, что человек, который с ним разговаривает, — все
инструменты ходят через ctx.actor, а не ctx.subject (ADR-0005).
"""
from app.agent.llm import LLMResponse
from app.agent.registry import ToolContext
from app.agent.runtime import Agent
from app.modules.memory import knowledge, stats
from app.modules.memory.models import BoardEntry, RIGHT_EDIT, RIGHT_VIEW, Section
from app.modules.memory.tools import (create_board, forget, read_board, recall,
                                      remember, track_board, write_entry)
from tests.conftest import FakeLLM


def ctx(db, user, subject=None) -> ToolContext:
    return ToolContext(db=db, actor=user, subject=subject or user)


def _board(db, user, section_name="Малыш", board_name="Кормления",
           instruction="Записи вида «время объём»: 170 — это миллилитры."):
    section = knowledge.create_section(db, user.id, section_name)
    return knowledge.create_board(db, user.id, section.id, board_name, instruction)


# --- read_board / recall: границы совпадают с человеческими -----------------------

def test_read_board_returns_entries_with_the_instruction(db, head):
    board = _board(db, head)
    knowledge.add_entry(db, head.id, board.id, "02:50 170")

    result = read_board(ctx(db, head), board="Кормления")

    assert result.ok
    assert "02:50 170" in result.summary
    assert "170 — это миллилитры" in result.summary


def test_read_board_does_not_reach_a_strangers_board(db, head, member):
    _board(db, member)

    result = read_board(ctx(db, head), board="Кормления")

    assert not result.ok
    assert "не нашёл" in result.summary


def test_recall_searches_all_reachable_boards_and_names_the_board(db, head, member):
    own = _board(db, head)
    shared = _board(db, member, section_name="Общий быт", board_name="Счётчики",
                    instruction="Показания по месяцам.")
    knowledge.share_board(db, member.id, shared.id, head.id, RIGHT_VIEW)
    knowledge.add_entry(db, head.id, own.id, "ночью съел 170 мл")
    knowledge.add_entry(db, member.id, shared.id, "за март 170 кВт")

    result = recall(ctx(db, head), query="170")

    boards = {hit["board"] for hit in result.data["hits"]}
    assert boards == {"Кормления", "Счётчики"}


def test_recall_without_a_keyword_lists_recent_entries(db, head):
    board = _board(db, head)
    knowledge.add_entry(db, head.id, board.id, "02:50 170")

    result = recall(ctx(db, head), query="")

    assert result.data["hits"] and result.data["hits"][0]["text"] == "02:50 170"


def test_recall_treats_like_metacharacters_as_letters(db, head):
    board = _board(db, head)
    knowledge.add_entry(db, head.id, board.id, "прошёл 1000 шагов")
    knowledge.add_entry(db, head.id, board.id, "заряд 100%")

    hits = recall(ctx(db, head), query="100%").data["hits"]

    assert [h["text"] for h in hits] == ["заряд 100%"]


def test_recall_only_reaches_what_this_person_can_see(db, head, member):
    board = _board(db, member)
    knowledge.add_entry(db, member.id, board.id, "личная запись про грибы")

    assert recall(ctx(db, head), query="грибы").data["hits"] == []


def test_tools_follow_the_speaker_not_the_viewed_avatar(db, head, member):
    """«От лица» не даёт ассистенту чужих досок: доступ идёт по actor (ADR-0005)."""
    board = _board(db, member)
    knowledge.add_entry(db, member.id, board.id, "запись Лёвы про грибы")

    # Глава семьи смотрит панель «от лица» Лёвы, но ассистент отвечает главе.
    result = recall(ctx(db, head, subject=member), query="грибы")

    assert result.data["hits"] == []


# --- write_entry: право и неоднозначность -----------------------------------------

def test_write_entry_is_refused_on_a_view_only_board(db, head, member):
    board = _board(db, member)
    knowledge.share_board(db, member.id, board.id, head.id, RIGHT_VIEW)

    result = write_entry(ctx(db, head), board="Кормления", text="02:50 170")

    assert not result.ok
    assert "просмотр" in result.summary
    assert knowledge.list_entries(db, member.id, board.id) == []


def test_write_entry_with_edit_right_lands_on_the_shared_board(db, head, member):
    board = _board(db, member)
    knowledge.share_board(db, member.id, board.id, head.id, RIGHT_EDIT)

    result = write_entry(ctx(db, head), board="Кормления", text="02:50 170")

    assert result.ok
    entry = knowledge.list_entries(db, member.id, board.id)[0]
    assert entry.text == "02:50 170"
    assert entry.author_id == head.id


def test_ambiguous_board_name_returns_options_instead_of_a_guess(db, head):
    section = knowledge.create_section(db, head.id, "Малыш")
    knowledge.create_board(db, head.id, section.id, "Кормления днём")
    knowledge.create_board(db, head.id, section.id, "Кормления ночью")

    result = write_entry(ctx(db, head), board="Кормления", text="170")

    assert not result.ok
    assert "Кормления днём" in result.summary and "Кормления ночью" in result.summary
    assert db.query(BoardEntry).count() == 0


# --- remember / forget: личная доска ассистента -----------------------------------

def test_remember_lazily_creates_the_assistants_own_board(db, head):
    result = remember(ctx(db, head), text="Соня не ест грибы")

    assert result.ok
    board = (db.query(Section).filter(Section.user_id == head.id,
                                      Section.name == "Личное").one())
    entries = db.query(BoardEntry).all()
    assert len(entries) == 1
    assert entries[0].by_assistant and entries[0].author_id is None

    # Второе запоминание не плодит вторую доску.
    remember(ctx(db, head), text="Лёва любит какао")
    assert db.query(Section).filter(Section.user_id == head.id).count() == 1


def test_remember_with_a_blank_text_leaves_no_empty_board_behind(db, head):
    result = remember(ctx(db, head), text="   ")

    assert not result.ok
    assert db.query(Section).filter(Section.user_id == head.id).count() == 0


def test_approving_someones_remember_keeps_the_fact_theirs(db, head, member):
    """«Да» главы семьи подтверждает чужую просьбу, но не переносит её данные
    к себе: действие исполняется в контексте того, чей это был разговор."""
    from app.agent.llm import LLMResponse, ToolCall
    from app.agent.runtime import Agent, approve_action
    from app.core.models import PendingAction

    member.autonomy = 0    # всё спрашивает
    db.commit()
    llm = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="remember",
                                         arguments={"text": "аллергия на орехи"})]),
        LLMResponse(content="Подготовил, подтвердите."),
    ])
    Agent(llm).respond(db, member, "запомни: аллергия на орехи")
    pending = db.query(PendingAction).one()

    approve_action(db, pending.id, head)

    section = db.query(Section).filter(Section.name == "Личное").one()
    assert section.user_id == member.id     # факт остался у Лёвы, не у Марины


def test_forget_removes_only_the_assistants_own_entry(db, head):
    remember(ctx(db, head), text="Соня не ест грибы")
    own = db.query(BoardEntry).one()
    board = _board(db, head)
    human = knowledge.add_entry(db, head.id, board.id, "запись человека")

    refused = forget(ctx(db, head), entry_id=human.id)
    assert not refused.ok
    assert knowledge.list_entries(db, head.id, board.id) != []

    removed = forget(ctx(db, head), entry_id=own.id)
    assert removed.ok
    assert db.query(BoardEntry).filter(BoardEntry.by_assistant).count() == 0


# --- create_board ------------------------------------------------------------------

def test_create_board_puts_the_board_into_the_named_section(db, head):
    knowledge.create_section(db, head.id, "Дом")

    result = create_board(ctx(db, head), section="Дом", name="Счётчики",
                          instruction="Показания по месяцам.")

    assert result.ok
    board = knowledge.find_boards_by_name(db, head.id, "Счётчики")[0].board
    assert board.instruction == "Показания по месяцам."


def test_create_board_refuses_an_unknown_section(db, head):
    result = create_board(ctx(db, head), section="Гараж", name="Машина")

    assert not result.ok
    assert "Гараж" in result.summary


# --- track_board: регулярная цифра по доске словами ---------------------------------

def _counted_board(db, user, **kwargs):
    """Доска со словарём величин: без словаря считать не по чему."""
    board = _board(db, user, **kwargs)
    knowledge.add_event_type(db, user.id, board.id, "кормление", "мл")
    return board


def test_track_board_sets_the_task_in_the_persons_own_words(db, head):
    board = _counted_board(db, head)

    result = track_board(ctx(db, head), board="Кормления", kind="кормление",
                         request="каждое утро — сколько малыш съел за сутки")

    assert result.ok
    task = stats.list_tasks(db, board.id)[0]
    assert task.request == "каждое утро — сколько малыш съел за сутки"
    assert task.kind == "кормление"
    assert task.digest_kind == "morning_digest"


def test_track_board_takes_the_only_kind_of_the_board_without_asking(db, head):
    board = _counted_board(db, head)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].kind == "кормление"


def test_track_board_asks_instead_of_guessing_an_unknown_kind(db, head):
    """«Кормление», «еда» и «молоко» не заводятся вперемешку — тип берётся из словаря."""
    board = _counted_board(db, head)
    knowledge.add_event_type(db, head.id, board.id, "срыгивание", "мл")

    result = track_board(ctx(db, head), board="Кормления", kind="еда",
                         request="сколько съел")

    assert not result.ok
    assert "кормление" in result.summary and "срыгивание" in result.summary
    assert stats.list_tasks(db, board.id) == []


def test_track_board_says_what_to_do_with_a_board_without_a_dictionary(db, head):
    board = _board(db, head, board_name="Мысли", instruction=None)

    result = track_board(ctx(db, head), board="Мысли", request="сколько мыслей")

    assert not result.ok
    assert "словар" in result.summary.lower()
    assert stats.list_tasks(db, board.id) == []


def test_track_board_works_on_a_board_shared_with_this_person(db, head, member):
    """Вопрос к общему логу не зависит от владельца доски."""
    board = _counted_board(db, member)
    knowledge.share_board(db, member.id, board.id, head.id, RIGHT_VIEW)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].author_id == head.id


def test_track_board_does_not_start_a_broadcast_on_a_board_that_is_not_yours(db, head, member):
    """Рассылку всем допущенным включает только владелец доски."""
    board = _counted_board(db, member)
    knowledge.share_board(db, member.id, board.id, head.id, RIGHT_EDIT)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки",
                         for_all=True)

    assert not result.ok
    assert "владелец" in result.summary
    assert stats.list_tasks(db, board.id) == []


def test_track_board_broadcasts_when_the_owner_asks_for_it(db, head):
    board = _counted_board(db, head)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки",
                         for_all=True)

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].share_all


def test_track_board_refuses_the_sixth_task_on_a_board(db, head):
    board = _counted_board(db, head)
    for number in range(stats.MAX_TASKS_PER_BOARD):
        track_board(ctx(db, head), board="Кормления", request=f"вопрос {number}")

    result = track_board(ctx(db, head), board="Кормления", request="лишний вопрос")

    assert not result.ok
    assert "пять" in result.summary
    assert len(stats.list_tasks(db, board.id)) == stats.MAX_TASKS_PER_BOARD


def test_track_board_does_not_reach_a_strangers_board(db, head, member):
    board = _counted_board(db, member)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки")

    assert not result.ok
    assert stats.list_tasks(db, board.id) == []


def test_track_board_warns_that_the_digest_itself_is_switched_off(db, head):
    """Своего расписания у задачи нет: молчащая сводка — молчащая цифра."""
    _counted_board(db, head)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert "выключена" in result.summary


def test_track_board_says_nothing_about_a_digest_that_is_on(db, head):
    from app.core.models import ScheduledJob

    _counted_board(db, head)
    db.add(ScheduledJob(user_id=head.id, kind="morning_digest", at_time="08:00", enabled=True))
    db.commit()

    result = track_board(ctx(db, head), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert "выключена" not in result.summary


def test_track_board_puts_the_weekly_question_into_the_weekly_review(db, head):
    board = _counted_board(db, head)

    result = track_board(ctx(db, head), board="Кормления", request="сколько за неделю",
                         digest="weekly_review")

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].digest_kind == "weekly_review"


# --- системный промпт --------------------------------------------------------------

def test_system_prompt_carries_board_names_and_instructions_but_not_contents(db, head):
    board = _board(db, head)
    knowledge.add_entry(db, head.id, board.id, "02:50 совершенно секретные 170 мл")

    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, head, "привет")

    system = llm.calls[0]["messages"][0]["content"]
    assert "Кормления" in system
    assert "170 — это миллилитры" in system          # инструкция — в промпте
    assert "совершенно секретные" not in system      # содержимое — только через read_board
