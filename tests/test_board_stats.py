"""Регулярная статистика доски: задача, прогон и ряд по дням (тикет #31).

Задача не заводит своего расписания — она цепляется к существующим сводкам,
поэтому прогон здесь всегда вызывается так же, как его вызовет планировщик.

Числа считает код: модель в этих тестах получает уже готовые цифры и отвечает
заранее заданной фразой, а по-настоящему не вызывается никогда.
"""
from datetime import date, datetime, timedelta

import pytest

from app.agent.llm import LLMUnavailable
from app.modules.memory import knowledge, stats
from app.modules.memory.models import BoardStatsTask, RIGHT_EDIT, RIGHT_VIEW
from tests.conftest import FakeLLM


def _parsed(*events):
    return {"events": [dict(event) for event in events]}


def _event(kind="кормление", at=None, value=170, unit="мл", confidence="high", raw="170"):
    return {"kind": kind, "at": at, "value": value, "unit": unit,
            "confidence": confidence, "raw": raw}


def _said(text="За сутки малыш съел 510 мл."):
    """Ответ модели: одна фраза по уже посчитанным числам."""
    return {"text": text}


@pytest.fixture
def board(db, member):
    section = knowledge.create_section(db, member.id, "Малыш")
    board = knowledge.create_board(db, member.id, section.id, "Кормления",
                                   instruction="Записи вида «время объём»: 170 — это миллилитры.")
    knowledge.add_event_type(db, member.id, board.id, "кормление", "мл")
    knowledge.add_event_type(db, member.id, board.id, "срыгивание", "мл")
    return board


def _log(db, user, board, text, **event):
    """Запись на доску вместе с её разбором на величины."""
    return knowledge.add_entry(db, user.id, board.id, text,
                               llm=FakeLLM([_parsed(_event(**event))]))


def _task(db, user, board, request="каждое утро — сколько малыш съел за сутки",
          kind="кормление", **kwargs):
    return stats.create_task(db, user.id, board.id, request=request, kind=kind, **kwargs)


# --- постановка задачи ----------------------------------------------------------

def test_a_task_is_set_by_the_words_of_the_person(db, member, board):
    task = _task(db, member, board)

    assert task is not None
    assert task.request == "каждое утро — сколько малыш съел за сутки"
    assert task.kind == "кормление"
    assert stats.list_tasks(db, board.id) == [task]


def test_a_task_is_set_on_any_board_you_can_reach(db, member, other, board):
    """Вопрос к общему логу не зависит от владельца доски."""
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)

    task = _task(db, other, board, request="сколько он съел")

    assert task is not None
    assert task.author_id == other.id


def test_a_stranger_does_not_set_a_task_on_a_board_he_cannot_see(db, member, other, board):
    assert _task(db, other, board) is None
    assert stats.list_tasks(db, board.id) == []


def test_a_kind_outside_the_dictionary_is_not_a_task(db, member, board):
    """Считать код умеет по типу из словаря доски — «съеденное вообще» ему не тип."""
    assert _task(db, member, board, kind="еда") is None


def test_the_kind_is_written_the_way_the_dictionary_writes_it(db, member, board):
    assert _task(db, member, board, kind="КОРМЛЕНИЕ").kind == "кормление"


def test_a_sixth_task_does_not_fit_on_one_board(db, member, board):
    """Пять задач — потолок: утренняя сводка не должна стать отчётом."""
    for number in range(stats.MAX_TASKS_PER_BOARD):
        assert _task(db, member, board, request=f"вопрос {number}") is not None

    with pytest.raises(stats.TooManyTasks):
        _task(db, member, board, request="лишний вопрос")

    assert len(stats.list_tasks(db, board.id)) == stats.MAX_TASKS_PER_BOARD


def test_the_ceiling_is_counted_per_board_and_not_per_family(db, member, board):
    other = knowledge.create_board(db, member.id, board.section_id, "Прогулки")
    knowledge.add_event_type(db, member.id, other.id, "прогулка", "мин")
    for number in range(stats.MAX_TASKS_PER_BOARD):
        _task(db, member, board, request=f"вопрос {number}")

    assert _task(db, member, other, request="сколько гуляли", kind="прогулка") is not None


# --- рассылка: только владелец ---------------------------------------------------

def test_only_the_owner_turns_the_broadcast_on(db, member, other, board):
    """Слать уведомления всей семье, ни с кем не согласовав, нельзя."""
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_EDIT)

    with pytest.raises(stats.NotTheOwner):
        _task(db, other, board, for_all=True)

    task = _task(db, other, board)
    assert not task.share_all
    assert not stats.set_broadcast(db, other.id, task.id, True)
    assert not task.share_all


def test_the_owner_turns_the_broadcast_on_for_a_task_he_did_not_set(db, member, other, board):
    """Задачу поставил допущенный, а рассылку по своей доске включает владелец."""
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_EDIT)
    task = _task(db, other, board)

    assert stats.set_broadcast(db, member.id, task.id, True)

    assert task.share_all


# --- прогон: числа считает код ---------------------------------------------------

def test_the_model_gets_the_numbers_ready_and_only_formulates(db, member, board):
    """Модель не складывает столбиком: ей приносят посчитанное."""
    _log(db, member, board, "02:50 170", value=170)
    _log(db, member, board, "06:10 180", value=180)
    _log(db, member, board, "09:30 160", value=160)
    task = _task(db, member, board)
    llm = FakeLLM([_said()])

    said = stats.run_task(db, task, llm=llm)

    asked = llm.calls[0]["user"]
    assert "510" in asked                                    # сумма посчитана кодом
    assert "каждое утро — сколько малыш съел за сутки" in asked   # и слова человека при ней
    assert said == "За сутки малыш съел 510 мл."


def test_an_uncertain_value_does_not_reach_the_regular_figure(db, member, board):
    _log(db, member, board, "02:50 170", value=170)
    _log(db, member, board, "потом ещё немного", value=40, confidence="low")
    task = _task(db, member, board)

    stats.run_task(db, task, llm=FakeLLM([_said()]))

    assert stats.series(db, task.id)[-1].value == 170.0


def test_another_type_of_the_same_board_is_not_added_to_the_figure(db, member, board):
    """Съеденное и потраченное — разные типы: одна задача считает свой."""
    _log(db, member, board, "02:50 170", value=170)
    _log(db, member, board, "срыгнул 30", kind="срыгивание", value=30)
    task = _task(db, member, board)

    stats.run_task(db, task, llm=FakeLLM([_said()]))

    assert stats.series(db, task.id)[-1].value == 170.0


def test_the_series_keeps_one_unit_and_the_phrase_names_the_rest(db, member, board):
    """Ряд — один показатель в одной единице: миллилитры и литры не лягут на одну ось."""
    _log(db, member, board, "02:50 170", value=170)
    _log(db, member, board, "днём 0.2 л", value=0.2, unit="л")
    task = _task(db, member, board)
    llm = FakeLLM([_said()])

    stats.run_task(db, task, llm=llm)

    point = stats.series(db, task.id)[-1]
    assert (point.value, point.unit) == (170.0, "мл")     # единица словаря доски
    assert "0.2 л" in llm.calls[0]["user"]                # но литры модель увидела


def test_what_happened_before_the_window_is_not_counted(db, member, board):
    old = _log(db, member, board, "давнее 500", value=500)
    knowledge.entry_events(db, old.id)[0].at = datetime.utcnow() - timedelta(days=3)
    db.commit()
    _log(db, member, board, "02:50 170", value=170)
    task = _task(db, member, board)

    stats.run_task(db, task, llm=FakeLLM([_said()]))

    assert stats.series(db, task.id)[-1].value == 170.0


def test_a_week_task_looks_a_week_back(db, member, board):
    older = _log(db, member, board, "давнее 500", value=500)
    knowledge.entry_events(db, older.id)[0].at = datetime.utcnow() - timedelta(days=3)
    db.commit()
    _log(db, member, board, "02:50 170", value=170)
    task = _task(db, member, board, digest_kind="weekly_review")

    stats.run_task(db, task, llm=FakeLLM([_said()]))

    assert stats.series(db, task.id)[-1].value == 670.0


def test_nothing_counted_is_not_a_figure(db, member, board):
    """Пустые сутки — не ноль в ряду: у табло «данных за 2 дня из 7» честнее нуля."""
    task = _task(db, member, board)

    assert stats.run_task(db, task, llm=FakeLLM([])) is None
    assert stats.series(db, task.id) == []


def test_the_figure_survives_a_model_that_did_not_answer(db, member, board):
    """Цифра важнее формулировки: посчитанное кодом доезжает и без модели."""
    class Dead:
        def json_completion(self, *args, **kwargs):
            raise LLMUnavailable("модель недоступна")

    _log(db, member, board, "02:50 170", value=170)
    task = _task(db, member, board)

    said = stats.run_task(db, task, llm=Dead())

    assert "170" in said
    assert stats.series(db, task.id)[-1].value == 170.0


# --- ряд по дням -----------------------------------------------------------------

def test_every_run_adds_a_point_to_the_series(db, member, board):
    """Каждый прогон — своя точка: день ряда считается по своим суткам, а не по всему логу."""
    task = _task(db, member, board)
    now = datetime.utcnow()
    yesterday = _log(db, member, board, "02:50 170", value=170)
    knowledge.entry_events(db, yesterday.id)[0].at = now - timedelta(hours=1)
    db.commit()

    stats.run_task(db, task, now=now, llm=FakeLLM([_said()]))

    today = _log(db, member, board, "03:00 180", value=180)
    knowledge.entry_events(db, today.id)[0].at = now + timedelta(hours=23)
    db.commit()

    stats.run_task(db, task, now=now + timedelta(days=1), llm=FakeLLM([_said()]))

    series = stats.series(db, task.id)
    assert [point.value for point in series] == [170.0, 180.0]
    assert series[-1].unit == "мл"


def test_a_second_run_on_the_same_day_does_not_double_the_series(db, member, board):
    _log(db, member, board, "02:50 170", value=170)
    task = _task(db, member, board)

    stats.run_task(db, task, llm=FakeLLM([_said()]))
    stats.run_task(db, task, llm=FakeLLM([_said()]))

    assert len(stats.series(db, task.id)) == 1


def test_the_series_dies_with_its_task(db, member, board):
    _log(db, member, board, "02:50 170", value=170)
    task = _task(db, member, board)
    stats.run_task(db, task, llm=FakeLLM([_said()]))
    task_id = task.id

    assert stats.delete_task(db, member.id, task_id)

    assert stats.series(db, task_id) == []


def test_a_task_dies_with_its_board(db, member, board):
    """Показатель не переживает лог, по которому считался."""
    task_id = _task(db, member, board).id

    knowledge.delete_board(db, member.id, board.id)

    assert stats.list_tasks(db, board.id) == []
    assert db.query(BoardStatsTask).filter(BoardStatsTask.id == task_id).count() == 0


def test_a_stranger_does_not_remove_your_task(db, member, other, board):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_EDIT)
    task = _task(db, member, board)

    assert not stats.delete_task(db, other.id, task.id)
    assert stats.list_tasks(db, board.id) == [task]


# --- кому это приезжает в сводку ---------------------------------------------------

def _digest(db, user, kind="morning_digest", said="За сутки малыш съел 170 мл."):
    return stats.digest_parts(db, user, kind, llm=FakeLLM([_said(said)]))


def test_the_figure_reaches_the_person_who_asked_for_it(db, member, board):
    _log(db, member, board, "02:50 170", value=170)
    _task(db, member, board)

    assert _digest(db, member) == ["За сутки малыш съел 170 мл."]


def test_without_the_broadcast_the_figure_stays_with_its_author(db, member, other, board):
    """Задача допущенного — его личное дело: семье он ничего не рассылает."""
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_EDIT)
    _log(db, member, board, "02:50 170", value=170)
    _task(db, other, board)

    assert _digest(db, other) == ["За сутки малыш съел 170 мл."]
    assert _digest(db, member) == []


def test_the_broadcast_reaches_everyone_allowed(db, member, other, board):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    _log(db, member, board, "02:50 170", value=170)
    _task(db, member, board, for_all=True)

    assert _digest(db, other) == ["За сутки малыш съел 170 мл."]


def test_a_revoked_access_takes_the_figure_away_with_the_board(db, member, other, board):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_EDIT)
    _log(db, member, board, "02:50 170", value=170)
    _task(db, other, board)

    knowledge.revoke_share(db, member.id, board.id, other.id)

    assert _digest(db, other) == []


def test_everyone_allowed_hears_the_same_number(db, member, other, board):
    """Сводки участников приходят в разное время, а цифра за день — одна (ADR-0002)."""
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    _log(db, member, board, "02:50 170", value=170)
    _task(db, member, board, for_all=True)
    assert _digest(db, member) == ["За сутки малыш съел 170 мл."]

    _log(db, member, board, "09:30 180", value=180)      # пока сводка шла к одному
    later = stats.digest_parts(db, other, "morning_digest",
                               llm=FakeLLM([_said("За сутки малыш съел 170 мл.")]))

    assert later == ["За сутки малыш съел 170 мл."]
    assert [point.value for point in stats.series(db, task_of(db, board))] == [170.0]


def task_of(db, board):
    return stats.list_tasks(db, board.id)[0].id


def test_a_morning_task_does_not_come_in_the_evening(db, member, board):
    _log(db, member, board, "02:50 170", value=170)
    _task(db, member, board)

    assert _digest(db, member, kind="evening_summary") == []


def test_the_digest_tells_how_many_entries_wait_for_a_clarification(db, member, board):
    """Одна фраза про необъяснённое: неполное не выдаётся за полное (ADR-0002)."""
    _log(db, member, board, "02:50 170", value=170)
    _log(db, member, board, "потом ещё немного", value=40, confidence="low")
    _task(db, member, board)

    parts = _digest(db, member)

    assert parts[0] == "За сутки малыш съел 170 мл."
    assert "1 запись ждёт уточнения" in parts[-1]
    assert f"board={board.id}" in parts[-1]      # и ссылка, по которой её видно


def test_nothing_to_tell_is_a_silent_digest(db, member, board):
    _task(db, member, board)

    assert stats.digest_parts(db, member, "morning_digest", llm=FakeLLM([])) == []


# --- ряд глазами табло ------------------------------------------------------------

def test_the_series_gives_back_the_days_asked_for(db, member, board):
    task = _task(db, member, board)
    now = datetime.utcnow()
    for days_back in (5, 3, 0):
        entry = _log(db, member, board, f"минус {days_back} дней — 170", value=170)
        # Величина ложится внутрь суток своего прогона, а не на их границу.
        knowledge.entry_events(db, entry.id)[0].at = now - timedelta(days=days_back, hours=1)
        db.commit()
        stats.run_task(db, task, now=now - timedelta(days=days_back), llm=FakeLLM([_said()]))

    recent = stats.series(db, task.id, days=4)

    assert len(recent) == 2
    assert recent[-1].day == date.today()
