"""Инструменты агента для досок: проекция полномочий человека на ассистента.

Ассистент видит ровно то же, что человек, который с ним разговаривает, — все
инструменты ходят через ctx.actor, а не ctx.subject (ADR-0005).
"""
from app.agent import policy
from app.agent.llm import LLMResponse
from app.agent.registry import ToolContext
from app.agent.runtime import Agent
from app.modules.memory import knowledge, screens, stats
from app.modules.memory.models import BoardEntry, RIGHT_EDIT, RIGHT_VIEW, Section
from app.modules.memory.tools import (create_board, forget, read_board, recall,
                                      remember, show_stats, track_board, write_entry)
from tests.conftest import FakeLLM


def ctx(db, user, subject=None) -> ToolContext:
    return ToolContext(db=db, actor=user, subject=subject or user)


def _board(db, user, section_name="Малыш", board_name="Кормления",
           instruction="Записи вида «время объём»: 170 — это миллилитры."):
    section = knowledge.create_section(db, user.id, section_name)
    return knowledge.create_board(db, user.id, section.id, board_name, instruction)


# --- read_board / recall: границы совпадают с человеческими -----------------------

def test_read_board_returns_entries_with_the_instruction(db, member):
    board = _board(db, member)
    knowledge.add_entry(db, member.id, board.id, "02:50 170")

    result = read_board(ctx(db, member), board="Кормления")

    assert result.ok
    assert "02:50 170" in result.summary
    assert "170 — это миллилитры" in result.summary


def test_read_board_does_not_reach_a_strangers_board(db, member, other):
    _board(db, other)

    result = read_board(ctx(db, member), board="Кормления")

    assert not result.ok
    assert "не нашёл" in result.summary


def test_recall_searches_all_reachable_boards_and_names_the_board(db, member, other):
    own = _board(db, member)
    shared = _board(db, other, section_name="Общий быт", board_name="Счётчики",
                    instruction="Показания по месяцам.")
    knowledge.share_board(db, other.id, shared.id, member.id, RIGHT_VIEW)
    knowledge.add_entry(db, member.id, own.id, "ночью съел 170 мл")
    knowledge.add_entry(db, other.id, shared.id, "за март 170 кВт")

    result = recall(ctx(db, member), query="170")

    boards = {hit["board"] for hit in result.data["hits"]}
    assert boards == {"Кормления", "Счётчики"}


def test_recall_without_a_keyword_lists_recent_entries(db, member):
    board = _board(db, member)
    knowledge.add_entry(db, member.id, board.id, "02:50 170")

    result = recall(ctx(db, member), query="")

    assert result.data["hits"] and result.data["hits"][0]["text"] == "02:50 170"


def test_recall_treats_like_metacharacters_as_letters(db, member):
    board = _board(db, member)
    knowledge.add_entry(db, member.id, board.id, "прошёл 1000 шагов")
    knowledge.add_entry(db, member.id, board.id, "заряд 100%")

    hits = recall(ctx(db, member), query="100%").data["hits"]

    assert [h["text"] for h in hits] == ["заряд 100%"]


def test_recall_only_reaches_what_this_person_can_see(db, member, other):
    board = _board(db, other)
    knowledge.add_entry(db, other.id, board.id, "личная запись про грибы")

    assert recall(ctx(db, member), query="грибы").data["hits"] == []


def test_tools_follow_the_speaker_not_the_viewed_avatar(db, member, other):
    """«От лица» не даёт ассистенту чужих досок: доступ идёт по actor (ADR-0005)."""
    board = _board(db, other)
    knowledge.add_entry(db, other.id, board.id, "запись Лёвы про грибы")

    # Глава семьи смотрит панель «от лица» Лёвы, но ассистент отвечает главе.
    result = recall(ctx(db, member, subject=other), query="грибы")

    assert result.data["hits"] == []


# --- write_entry: право и неоднозначность -----------------------------------------

def test_write_entry_is_refused_on_a_view_only_board(db, member, other):
    board = _board(db, other)
    knowledge.share_board(db, other.id, board.id, member.id, RIGHT_VIEW)

    result = write_entry(ctx(db, member), board="Кормления", text="02:50 170")

    assert not result.ok
    assert "просмотр" in result.summary
    assert knowledge.list_entries(db, other.id, board.id) == []


def test_write_entry_with_edit_right_lands_on_the_shared_board(db, member, other):
    board = _board(db, other)
    knowledge.share_board(db, other.id, board.id, member.id, RIGHT_EDIT)

    result = write_entry(ctx(db, member), board="Кормления", text="02:50 170")

    assert result.ok
    entry = knowledge.list_entries(db, other.id, board.id)[0]
    assert entry.text == "02:50 170"
    assert entry.author_id == member.id


def test_ambiguous_board_name_returns_options_instead_of_a_guess(db, member):
    section = knowledge.create_section(db, member.id, "Малыш")
    knowledge.create_board(db, member.id, section.id, "Кормления днём")
    knowledge.create_board(db, member.id, section.id, "Кормления ночью")

    result = write_entry(ctx(db, member), board="Кормления", text="170")

    assert not result.ok
    assert "Кормления днём" in result.summary and "Кормления ночью" in result.summary
    assert db.query(BoardEntry).count() == 0


# --- remember / forget: личная доска ассистента -----------------------------------

def test_remember_lazily_creates_the_assistants_own_board(db, member):
    result = remember(ctx(db, member), text="Соня не ест грибы")

    assert result.ok
    board = (db.query(Section).filter(Section.user_id == member.id,
                                      Section.name == "Личное").one())
    entries = db.query(BoardEntry).all()
    assert len(entries) == 1
    assert entries[0].by_assistant and entries[0].author_id is None

    # Второе запоминание не плодит вторую доску.
    remember(ctx(db, member), text="Лёва любит какао")
    assert db.query(Section).filter(Section.user_id == member.id).count() == 1


def test_remember_with_a_blank_text_leaves_no_empty_board_behind(db, member):
    result = remember(ctx(db, member), text="   ")

    assert not result.ok
    assert db.query(Section).filter(Section.user_id == member.id).count() == 0


def test_only_the_person_themselves_confirms_their_action(db, member, other):
    """Чужое «да» больше не считается: подтверждает тот, чей это был разговор.

    Раньше это умел глава семьи — роль разделилась на администратора и
    участника, и подтверждать за другого стало некому (ADR-0007).
    """
    from app.agent.llm import LLMResponse, ToolCall
    from app.agent.runtime import Agent, approve_action
    from app.core.models import PendingAction

    policy.set_autonomy(db, other.family_id, 0)    # всё спрашивает
    llm = FakeLLM([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="remember",
                                         arguments={"text": "аллергия на орехи"})]),
        LLMResponse(content="Подготовил, подтвердите."),
    ])
    Agent(llm).respond(db, other, "запомни: аллергия на орехи")
    pending = db.query(PendingAction).one()

    refused = approve_action(db, pending.id, member)
    assert not refused.ok
    assert db.query(Section).filter(Section.name == "Личное").count() == 0

    approve_action(db, pending.id, other)

    section = db.query(Section).filter(Section.name == "Личное").one()
    assert section.user_id == other.id     # факт остался у того, кто просил


def test_forget_removes_only_the_assistants_own_entry(db, member):
    remember(ctx(db, member), text="Соня не ест грибы")
    own = db.query(BoardEntry).one()
    board = _board(db, member)
    human = knowledge.add_entry(db, member.id, board.id, "запись человека")

    refused = forget(ctx(db, member), entry_id=human.id)
    assert not refused.ok
    assert knowledge.list_entries(db, member.id, board.id) != []

    removed = forget(ctx(db, member), entry_id=own.id)
    assert removed.ok
    assert db.query(BoardEntry).filter(BoardEntry.by_assistant).count() == 0


# --- create_board ------------------------------------------------------------------

def test_create_board_puts_the_board_into_the_named_section(db, member):
    knowledge.create_section(db, member.id, "Дом")

    result = create_board(ctx(db, member), section="Дом", name="Счётчики",
                          instruction="Показания по месяцам.")

    assert result.ok
    board = knowledge.find_boards_by_name(db, member.id, "Счётчики")[0].board
    assert board.instruction == "Показания по месяцам."


def test_create_board_refuses_an_unknown_section(db, member):
    result = create_board(ctx(db, member), section="Гараж", name="Машина")

    assert not result.ok
    assert "Гараж" in result.summary


# --- track_board: регулярная цифра по доске словами ---------------------------------

def _counted_board(db, user, **kwargs):
    """Доска со словарём величин: без словаря считать не по чему."""
    board = _board(db, user, **kwargs)
    knowledge.add_event_type(db, user.id, board.id, "кормление", "мл")
    return board


def test_track_board_sets_the_task_in_the_persons_own_words(db, member):
    board = _counted_board(db, member)

    result = track_board(ctx(db, member), board="Кормления", kind="кормление",
                         request="каждое утро — сколько малыш съел за сутки")

    assert result.ok
    task = stats.list_tasks(db, board.id)[0]
    assert task.request == "каждое утро — сколько малыш съел за сутки"
    assert task.kind == "кормление"
    assert task.digest_kind == "morning_digest"


def test_track_board_takes_the_only_kind_of_the_board_without_asking(db, member):
    board = _counted_board(db, member)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].kind == "кормление"


def test_track_board_asks_instead_of_guessing_an_unknown_kind(db, member):
    """«Кормление», «еда» и «молоко» не заводятся вперемешку — тип берётся из словаря."""
    board = _counted_board(db, member)
    knowledge.add_event_type(db, member.id, board.id, "срыгивание", "мл")

    result = track_board(ctx(db, member), board="Кормления", kind="еда",
                         request="сколько съел")

    assert not result.ok
    assert "кормление" in result.summary and "срыгивание" in result.summary
    assert stats.list_tasks(db, board.id) == []


def test_track_board_says_what_to_do_with_a_board_without_a_dictionary(db, member):
    board = _board(db, member, board_name="Мысли", instruction=None)

    result = track_board(ctx(db, member), board="Мысли", request="сколько мыслей")

    assert not result.ok
    assert "словар" in result.summary.lower()
    assert stats.list_tasks(db, board.id) == []


def test_track_board_works_on_a_board_shared_with_this_person(db, member, other):
    """Вопрос к общему логу не зависит от владельца доски."""
    board = _counted_board(db, other)
    knowledge.share_board(db, other.id, board.id, member.id, RIGHT_VIEW)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].author_id == member.id


def test_track_board_does_not_start_a_broadcast_on_a_board_that_is_not_yours(db, member, other):
    """Рассылку всем допущенным включает только владелец доски."""
    board = _counted_board(db, other)
    knowledge.share_board(db, other.id, board.id, member.id, RIGHT_EDIT)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки",
                         for_all=True)

    assert not result.ok
    assert "владелец" in result.summary
    assert stats.list_tasks(db, board.id) == []


def test_track_board_broadcasts_when_the_owner_asks_for_it(db, member):
    board = _counted_board(db, member)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки",
                         for_all=True)

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].share_all


def test_track_board_refuses_the_sixth_task_on_a_board(db, member):
    board = _counted_board(db, member)
    for number in range(stats.MAX_TASKS_PER_BOARD):
        track_board(ctx(db, member), board="Кормления", request=f"вопрос {number}")

    result = track_board(ctx(db, member), board="Кормления", request="лишний вопрос")

    assert not result.ok
    assert "пять" in result.summary
    assert len(stats.list_tasks(db, board.id)) == stats.MAX_TASKS_PER_BOARD


def test_track_board_does_not_reach_a_strangers_board(db, member, other):
    board = _counted_board(db, other)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки")

    assert not result.ok
    assert stats.list_tasks(db, board.id) == []


def test_track_board_warns_that_the_digest_itself_is_switched_off(db, member):
    """Своего расписания у задачи нет: молчащая сводка — молчащая цифра."""
    _counted_board(db, member)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert "выключена" in result.summary


def test_track_board_says_nothing_about_a_digest_that_is_on(db, member):
    from app.core.models import ScheduledJob

    _counted_board(db, member)
    db.add(ScheduledJob(user_id=member.id, kind="morning_digest", at_time="08:00", enabled=True))
    db.commit()

    result = track_board(ctx(db, member), board="Кормления", request="сколько за сутки")

    assert result.ok
    assert "выключена" not in result.summary


def test_track_board_puts_the_weekly_question_into_the_weekly_review(db, member):
    board = _counted_board(db, member)

    result = track_board(ctx(db, member), board="Кормления", request="сколько за неделю",
                         digest="weekly_review")

    assert result.ok
    assert stats.list_tasks(db, board.id)[0].digest_kind == "weekly_review"


# --- show_stats: табло по уже посчитанному ряду (тикет #32) --------------------------

def _tracked_board(db, user, **kwargs):
    """Доска, по которой уже считается показатель: из него и растёт табло."""
    board = _counted_board(db, user, **kwargs)
    stats.create_task(db, user.id, board.id, request="сколько малыш съел за сутки",
                      kind="кормление")
    return board


def test_show_stats_makes_a_screen_out_of_the_series(db, member):
    _tracked_board(db, member)

    result = show_stats(ctx(db, member), board="Кормления", name="Молоко за сутки", form="bars")

    assert result.ok
    screen = screens.list_screens(db, member.id)[0]
    assert (screen.name, screen.form) == ("Молоко за сутки", "bars")
    assert result.card["url"] == f"/stats/{screen.id}"


def test_show_stats_has_nothing_to_show_without_a_task(db, member):
    """Табло не считает само: без задачи статистики показывать нечего."""
    _counted_board(db, member)

    result = show_stats(ctx(db, member), board="Кормления", name="Молоко")

    assert not result.ok
    assert "track_board" in result.summary
    assert screens.list_screens(db, member.id) == []


def test_show_stats_asks_which_of_several_figures_to_show(db, member):
    board = _tracked_board(db, member)
    knowledge.add_event_type(db, member.id, board.id, "срыгивание", "мл")
    stats.create_task(db, member.id, board.id, request="сколько срыгнул", kind="срыгивание")

    result = show_stats(ctx(db, member), board="Кормления", name="Молоко")

    assert not result.ok
    assert "срыгивание" in result.summary
    assert screens.list_screens(db, member.id) == []


def test_show_stats_does_not_call_one_figure_several(db, member):
    """Названный тип не нашёлся — это не «их несколько»: показать один и сказать
    «выбирай» значит соврать."""
    _tracked_board(db, member)

    result = show_stats(ctx(db, member), board="Кормления", name="Молоко", kind="прогулка")

    assert not result.ok
    assert "«прогулка»" in result.summary
    assert "несколько" not in result.summary
    assert "кормление" in result.summary


def test_show_stats_takes_the_kind_when_it_is_named(db, member):
    board = _tracked_board(db, member)
    knowledge.add_event_type(db, member.id, board.id, "срыгивание", "мл")
    stats.create_task(db, member.id, board.id, request="сколько срыгнул", kind="срыгивание")

    result = show_stats(ctx(db, member), board="Кормления", name="Срыгивания", kind="срыгивание")

    assert result.ok
    assert stats.get_task(db, member.id, screens.list_screens(db, member.id)[0].task_id).kind \
        == "срыгивание"


def test_show_stats_corrects_the_form_of_an_existing_screen(db, member):
    """«Покажи это столбиками» — поправка табло, а не второй экран."""
    _tracked_board(db, member)
    show_stats(ctx(db, member), board="Кормления", name="Молоко", form="number")

    result = show_stats(ctx(db, member), board="Кормления", name="Молоко", form="bars")

    assert result.ok
    assert [s.form for s in screens.list_screens(db, member.id)] == ["bars"]


def test_show_stats_does_not_hang_a_fourth_screen_in_the_menu(db, member):
    for number in range(screens.MAX_SCREENS):
        board = _tracked_board(db, member, section_name=f"Раздел {number}",
                               board_name=f"Доска {number}")
        show_stats(ctx(db, member), board=board.name, name=f"Табло {number}")
    _tracked_board(db, member, section_name="Ещё", board_name="Прогулки")

    result = show_stats(ctx(db, member), board="Прогулки", name="Лишнее")

    assert not result.ok
    assert "три табло" in result.summary
    assert len(screens.list_screens(db, member.id)) == screens.MAX_SCREENS


def test_show_stats_does_not_reach_a_strangers_figure(db, member, other):
    _tracked_board(db, other)

    result = show_stats(ctx(db, member), board="Кормления", name="Чужое")

    assert not result.ok
    assert screens.list_screens(db, member.id) == []


# --- системный промпт --------------------------------------------------------------

def test_system_prompt_carries_board_names_and_instructions_but_not_contents(db, member):
    board = _board(db, member)
    knowledge.add_entry(db, member.id, board.id, "02:50 совершенно секретные 170 мл")

    llm = FakeLLM([LLMResponse(content="Привет!")])
    Agent(llm).respond(db, member, "привет")

    system = llm.calls[0]["messages"][0]["content"]
    assert "Кормления" in system
    assert "170 — это миллилитры" in system          # инструкция — в промпте
    assert "совершенно секретные" not in system      # содержимое — только через read_board
