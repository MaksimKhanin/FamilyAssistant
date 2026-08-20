"""Табло — экран одного показателя по задаче статистики (тикет #32, спека #19).

Числа табло считает код при каждом показе — по событиям доски и по календарным
дням семьи (ADR-0013), не дожидаясь прогонов сводок. Поэтому здесь проверяются
и границы (кому табло видно, сколько их влезает, переживает ли оно задачу), и
точность: чей день у события, доезжают ли поздние записи и уточнения, как
складываются литры с миллилитрами.
"""
from datetime import time, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.clock import local_today
from app.core.db import get_db
from app.main import app
from app.modules.memory import knowledge, screens, stats
from app.modules.memory.models import BoardStatsScreen, RIGHT_VIEW
from tests.conftest import FakeLLM


@pytest.fixture
def board(db, member):
    section = knowledge.create_section(db, member.id, "Малыш")
    board = knowledge.create_board(db, member.id, section.id, "Кормления")
    knowledge.add_event_type(db, member.id, board.id, "кормление", "мл")
    return board


@pytest.fixture
def task(db, member, board):
    return stats.create_task(db, member.id, board.id, request="сколько малыш съел за сутки",
                             kind="кормление")


def _event_on(db, member, board, day, value, unit="мл", kind="кормление",
              confidence="high", when=time(12, 0)):
    """Запись доски с событием на данный календарный день семьи — тем же путём,
    каким события появляются по-настоящему: через разбор записи."""
    at = f"{day:%Y-%m-%d} {when:%H:%M}"
    parsed = {"events": [{"kind": kind, "at": at, "value": value, "unit": unit,
                          "confidence": confidence, "raw": str(value)}]}
    return knowledge.add_entry(db, member.id, board.id, f"{at} {value}",
                               llm=FakeLLM([parsed]))


def _series(db, member, board, *values, unit="мл", last_day=None):
    """Готовый ряд: по событию на день, последняя величина — сегодня."""
    last_day = last_day or local_today()
    for offset, value in enumerate(reversed(values)):
        _event_on(db, member, board, last_day - timedelta(days=offset), value, unit=unit)


# --- заведение табло ------------------------------------------------------------

def test_a_screen_is_made_over_a_series_and_gets_its_name(db, member, task):
    screen = screens.create_screen(db, member.id, task.id, "Молоко за сутки")

    assert screen is not None
    assert screen.name == "Молоко за сутки"
    assert screens.list_screens(db, member.id) == [screen]


def test_a_screen_without_a_name_is_not_made(db, member, task):
    """Табло — пункт навигации: пункт без подписи некуда повесить."""
    assert screens.create_screen(db, member.id, task.id, "   ") is None
    assert screens.list_screens(db, member.id) == []


def test_an_overlong_name_is_cut_to_the_column_limit(db, member, task):
    screen = screens.create_screen(db, member.id, task.id, "х" * 300)

    assert len(screen.name) == screens.NAME_LIMIT


def test_a_fourth_screen_does_not_fit(db, member, board, task):
    """Три табло на человека: навигация — не витрина показателей."""
    for number in range(screens.MAX_SCREENS):
        extra = stats.create_task(db, member.id, board.id, request=f"вопрос {number}",
                                  kind="кормление")
        assert screens.create_screen(db, member.id, extra.id, f"Табло {number}") is not None

    with pytest.raises(screens.TooManyScreens):
        screens.create_screen(db, member.id, task.id, "Лишнее")

    assert len(screens.list_screens(db, member.id)) == screens.MAX_SCREENS


def test_the_ceiling_is_counted_per_person_and_not_per_family(db, member, other, board, task):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    stats.set_broadcast(db, member.id, task.id, True)
    for number in range(screens.MAX_SCREENS):
        extra = stats.create_task(db, member.id, board.id, request=f"вопрос {number}",
                                  kind="кормление")
        screens.create_screen(db, member.id, extra.id, f"Табло {number}")

    assert screens.create_screen(db, other.id, task.id, "Своё табло") is not None


def test_a_series_you_cannot_see_does_not_become_your_screen(db, other, task):
    assert screens.create_screen(db, other.id, task.id, "Чужое") is None
    assert screens.list_screens(db, other.id) == []


# --- вид: выбор из четырёх готовых форм ------------------------------------------

def test_the_form_is_one_of_four_ready_ones(db, member, task):
    """Разметку табло не генерирует модель: она выбирает из готового."""
    screen = screens.create_screen(db, member.id, task.id, "Молоко", form="bars")
    assert screen.form == "bars"

    outlandish = screens.create_screen(db, member.id, task.id, "Молоко", form="пироги")
    assert outlandish.form in screens.FORMS


def test_the_form_is_corrected_afterwards(db, member, task):
    """Вид ассистент предлагает сам, человек правит его потом."""
    screen = screens.create_screen(db, member.id, task.id, "Молоко", form="number")

    assert screens.set_form(db, member.id, screen.id, "table")

    assert screens.get_screen(db, member.id, screen.id).form == "table"


def test_a_stranger_does_not_touch_your_screen(db, member, other, task):
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert not screens.set_form(db, other.id, screen.id, "table")
    assert not screens.delete_screen(db, other.id, screen.id)
    assert screens.get_screen(db, other.id, screen.id) is None


def test_a_second_screen_over_the_same_series_corrects_the_first(db, member, task):
    """«Поправь словами» не должно съедать один из трёх слотов."""
    first = screens.create_screen(db, member.id, task.id, "Молоко", form="number")

    again = screens.create_screen(db, member.id, task.id, "Молоко за сутки", form="bars")

    assert again.id == first.id
    assert (again.name, again.form) == ("Молоко за сутки", "bars")
    assert len(screens.list_screens(db, member.id)) == 1


# --- кому табло видно -------------------------------------------------------------

def test_a_broadcast_series_becomes_a_screen_of_everyone_allowed(db, member, other, board, task):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    stats.set_broadcast(db, member.id, task.id, True)

    screen = screens.create_screen(db, other.id, task.id, "Сколько съел")

    assert screens.list_screens(db, other.id) == [screen]


def test_a_switched_off_broadcast_takes_the_screen_away(db, member, other, board, task):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    stats.set_broadcast(db, member.id, task.id, True)
    screens.create_screen(db, other.id, task.id, "Сколько съел")

    stats.set_broadcast(db, member.id, task.id, False)

    assert screens.list_screens(db, other.id) == []


def test_a_returned_access_does_not_hang_a_fourth_item_in_the_menu(db, member, other, board, task):
    """Доступ к ряду могут вернуть — и вчера незаметное табло всплыло бы четвёртым."""
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    stats.set_broadcast(db, member.id, task.id, True)
    screens.create_screen(db, other.id, task.id, "Общее")
    stats.set_broadcast(db, member.id, task.id, False)

    own = knowledge.create_section(db, other.id, "Своё")
    for number in range(screens.MAX_SCREENS):
        mine = knowledge.create_board(db, other.id, own.id, f"Доска {number}")
        knowledge.add_event_type(db, other.id, mine.id, "шаги", "шт")
        extra = stats.create_task(db, other.id, mine.id, request="сколько", kind="шаги")
        screens.create_screen(db, other.id, extra.id, f"Табло {number}")
    stats.set_broadcast(db, member.id, task.id, True)

    assert len(screens.list_screens(db, other.id)) == screens.MAX_SCREENS
    assert len(screens.nav_items(db, other)) == screens.MAX_SCREENS


def test_a_revoked_access_takes_the_screen_away_with_the_board(db, member, other, board, task):
    knowledge.share_board(db, member.id, board.id, other.id, RIGHT_VIEW)
    stats.set_broadcast(db, member.id, task.id, True)
    screens.create_screen(db, other.id, task.id, "Сколько съел")

    knowledge.revoke_share(db, member.id, board.id, other.id)

    assert screens.list_screens(db, other.id) == []


# --- табло живёт ровно столько, сколько живёт ряд ----------------------------------

def test_a_screen_dies_with_its_task(db, member, task):
    screen_id = screens.create_screen(db, member.id, task.id, "Молоко").id

    assert stats.delete_task(db, member.id, task.id)

    assert db.query(BoardStatsScreen).filter(BoardStatsScreen.id == screen_id).count() == 0


def test_a_screen_dies_with_its_board(db, member, board, task):
    """Показатель не переживает лог, по которому считался, — и табло тоже."""
    screen_id = screens.create_screen(db, member.id, task.id, "Молоко").id

    knowledge.delete_board(db, member.id, board.id)

    assert db.query(BoardStatsScreen).filter(BoardStatsScreen.id == screen_id).count() == 0


def test_a_screen_is_taken_off_by_hand(db, member, task):
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert screens.delete_screen(db, member.id, screen.id)

    assert screens.list_screens(db, member.id) == []


# --- что показывает табло ----------------------------------------------------------

def test_the_screen_shows_the_last_value_and_its_delta(db, member, board, task):
    _series(db, member, board, 500.0, 620.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert (view["last"], view["delta"], view["unit"]) == (620.0, 120.0, "мл")


def test_a_single_day_has_nothing_to_compare_with(db, member, board, task):
    _series(db, member, board, 500.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert screens.screen_view(db, member.id, screen)["delta"] is None


def test_the_delta_names_the_day_it_was_measured_against(db, member, board, task):
    """В ряду бывают дыры: «ко вчерашнему» на разнице с позавчерашним — неправда."""
    today = local_today()
    _event_on(db, member, board, today - timedelta(days=4), 500.0)
    _event_on(db, member, board, today, 620.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["delta_from"] == today - timedelta(days=4)


def test_a_single_point_still_draws_a_line(db, member, board, task):
    """Ломаная из одной точки не рисуется вовсе — экран выглядел бы пустым."""
    _series(db, member, board, 500.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко", form="line")

    assert len(screens.screen_view(db, member.id, screen)["line"].split()) == 2


def test_a_short_series_says_how_many_days_it_has(db, member, board, task):
    """Неполное не выдаётся за полное: «данных за N дней из M» (ADR-0002)."""
    _series(db, member, board, 500.0, 620.0, 480.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert (view["days_have"], view["days_asked"]) == (3, screens.WINDOW_DAYS)
    assert view["short"]


def test_a_full_window_does_not_apologise_for_itself(db, member, board, task):
    _series(db, member, board, *[500.0] * screens.WINDOW_DAYS)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert not screens.screen_view(db, member.id, screen)["short"]


def test_what_fell_out_of_the_window_is_not_shown(db, member, board, task):
    _series(db, member, board, *[500.0] * (screens.WINDOW_DAYS + 5))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["days_have"] == screens.WINDOW_DAYS


def test_an_empty_series_is_an_honest_emptiness_and_not_a_zero(db, member, task):
    """Пустые сутки — не ноль: табло молчит, а не рисует провал, которого не было."""
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["points"] == []
    assert view["last"] is None


# --- точность: табло считает по событиям, а не по прогонам сводок (ADR-0013) --------

def test_two_events_of_one_day_are_one_bar(db, member, board, task):
    """Столбик — календарный день семьи: два кормления одних суток складываются."""
    today = local_today()
    _event_on(db, member, board, today, 170.0, when=time(2, 50))
    _event_on(db, member, board, today, 200.0, when=time(6, 10))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert [point["value"] for point in view["points"]] == [370.0]


def test_an_event_lands_on_its_own_day_and_not_on_the_day_of_writing(db, member, board, task):
    """Дописанное задним числом ложится в свой день: вчерашний ужин — вчера."""
    yesterday = local_today() - timedelta(days=1)
    _event_on(db, member, board, yesterday, 170.0, when=time(23, 0))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert [point["day"] for point in view["points"]] == [yesterday]


def test_the_screen_does_not_wait_for_a_digest_run(db, member, board, task):
    """Раньше ряд копился только прогонами сводок — у выключенной сводки табло
    оставалось пустым навсегда. Теперь цифра есть, как только есть события."""
    _event_on(db, member, board, local_today(), 170.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["last"] == 170.0
    assert stats.series(db, task.id) == []      # снимков сводки при этом нет


def test_what_was_written_after_the_digest_reaches_the_screen(db, member, board, task):
    """Сводка — снимок своего момента, табло — данные: поздняя запись меняет
    столбик, но не переписывает уже разосланную цифру (ADR-0013)."""
    _event_on(db, member, board, local_today(), 170.0, when=time(2, 50))
    stats.run_task(db, task, llm=FakeLLM([{"text": "170 мл."}]))

    _event_on(db, member, board, local_today(), 200.0, when=time(6, 10))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert screens.screen_view(db, member.id, screen)["last"] == 370.0
    assert stats.series(db, task.id)[-1].value == 170.0


def test_a_clarified_value_reaches_the_screen(db, member, board, task):
    """Ответ на плашку уточнения доводит величину до ряда — в её собственный день."""
    entry = _event_on(db, member, board, local_today(), 40.0, kind="что-то",
                      confidence="low")
    screen = screens.create_screen(db, member.id, task.id, "Молоко")
    assert screens.screen_view(db, member.id, screen)["last"] is None

    event = knowledge.entry_events(db, entry.id)[0]
    knowledge.clarify_event(db, member.id, event.id, "кормление")

    assert screens.screen_view(db, member.id, screen)["last"] == 40.0


def test_litres_and_millilitres_lie_on_one_axis(db, member, board, task):
    """«0.2 л» и «170 мл» — одна величина, записанная по-разному: пересчёт точный,
    и на оси ряда они складываются в 370 мл."""
    today = local_today()
    _event_on(db, member, board, today, 170.0, when=time(2, 50))
    _event_on(db, member, board, today, 0.2, unit="л", when=time(6, 10))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert (view["last"], view["unit"]) == (370.0, "мл")


def test_a_foreign_unit_is_named_but_not_drawn(db, member, board, task):
    """«2 шт» в миллилитры не пересчитать ничем: в ось они не ложатся, но табло
    их называет, а не теряет молча (ADR-0002)."""
    today = local_today()
    _event_on(db, member, board, today, 170.0)
    _event_on(db, member, board, today, 2.0, unit="шт", when=time(15, 0))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["last"] == 170.0
    assert view["stray"] == [{"unit": "шт", "total": 2.0, "count": 1}]


def test_the_running_day_is_marked_as_still_going(db, member, board, task):
    """Сегодняшнее число ещё растёт — выдавать его за итог дня нельзя (ADR-0002)."""
    _event_on(db, member, board, local_today(), 170.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert screens.screen_view(db, member.id, screen)["today"]


def test_a_finished_day_is_not_marked_as_running(db, member, board, task):
    _event_on(db, member, board, local_today() - timedelta(days=1), 170.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert not screens.screen_view(db, member.id, screen)["today"]


# --- пункт навигации у каждого табло ------------------------------------------------

def test_every_screen_brings_its_own_nav_item(db, member, task):
    screens.create_screen(db, member.id, task.id, "Молоко за сутки")

    items = screens.nav_items(db, member)

    assert [item.label for item in items] == ["Молоко за сутки"]
    assert items[0].url.endswith(str(screens.list_screens(db, member.id)[0].id))


def test_the_module_hands_the_dynamic_items_to_the_panel(db, member, task):
    """Новый контракт модуля: пункты, которых нет в коде, — их завёл человек."""
    from app.modules.memory import module

    screens.create_screen(db, member.id, task.id, "Молоко за сутки")

    assert module.nav_items_for is not None
    assert [item.label for item in module.nav_items_for(db, member)] == ["Молоко за сутки"]


# --- экран табло ---------------------------------------------------------------------

@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_member(client, member):
    client.post("/login", data={"username": member.username, "password": "pw"},
                follow_redirects=False)
    return client


@pytest.fixture
def as_other(client, db, other):
    """Второй участник — им проверяется, что чужого табло попросту нет."""
    other.password_hash = hash_password("pw")
    db.commit()
    client.post("/login", data={"username": other.username, "password": "pw"},
                follow_redirects=False)
    return client


def test_the_screen_opens_by_its_own_address(db, member, board, task, as_member):
    _series(db, member, board, 500.0, 620.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко за сутки")

    page = as_member.get(f"/stats/{screen.id}")

    assert page.status_code == 200
    assert "Молоко за сутки" in page.text
    assert "620" in page.text


def test_the_short_series_is_named_on_the_screen(db, member, board, task, as_member):
    _series(db, member, board, 500.0, 620.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    page = as_member.get(f"/stats/{screen.id}")

    assert "Данных за 2 дня" in page.text
    assert str(screens.WINDOW_DAYS) in page.text


def test_the_screen_appears_in_the_navigation_of_every_screen(db, member, task, as_member):
    screen = screens.create_screen(db, member.id, task.id, "Молоко за сутки")

    page = as_member.get("/memory")

    assert f'href="/stats/{screen.id}"' in page.text
    assert "Молоко за сутки" in page.text


def test_someone_elses_screen_is_not_found(db, member, task, as_other):
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert as_other.get(f"/stats/{screen.id}").status_code == 404


def test_the_form_is_switched_from_the_screen(db, member, task, as_member):
    screen = screens.create_screen(db, member.id, task.id, "Молоко", form="number")

    response = as_member.post(f"/stats/{screen.id}/form", data={"form": "bars"},
                            follow_redirects=False)

    assert response.status_code == 303
    db.expire_all()
    assert screens.get_screen(db, member.id, screen.id).form == "bars"


def test_the_screen_is_taken_off_from_itself(db, member, task, as_member):
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    response = as_member.post(f"/stats/{screen.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert screens.list_screens(db, member.id) == []
