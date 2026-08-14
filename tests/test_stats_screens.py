"""Табло — экран одного показателя по ряду задачи статистики (тикет #32, спека #19).

Табло ничего не считает: числа посчитала задача статистики (#31), а табло только
показывает накопленный ею ряд. Поэтому здесь проверяется не арифметика, а границы:
кому табло видно, сколько их влезает человеку, что оно говорит на коротком ряде
и переживает ли оно задачу, по которой заведено.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.auth import hash_password
from app.core.db import get_db
from app.main import app
from app.modules.memory import knowledge, screens, stats
from app.modules.memory.models import BoardStatsScreen, RIGHT_VIEW


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


def _series(db, task, *values, unit="мл", last_day=None):
    """Готовый ряд: по точке на день, последняя — сегодня."""
    last_day = last_day or date.today()
    for offset, value in enumerate(reversed(values)):
        stats.record_point(db, task, last_day - timedelta(days=offset), value, unit)
    return task


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

def test_the_screen_shows_the_last_value_and_its_delta(db, member, task):
    _series(db, task, 500.0, 620.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert (view["last"], view["delta"], view["unit"]) == (620.0, 120.0, "мл")


def test_a_single_day_has_nothing_to_compare_with(db, member, task):
    _series(db, task, 500.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert screens.screen_view(db, member.id, screen)["delta"] is None


def test_the_delta_names_the_day_it_was_measured_against(db, member, task):
    """В ряду бывают дыры: «ко вчерашнему» на разнице с позавчерашним — неправда."""
    today = date.today()
    stats.record_point(db, task, today - timedelta(days=4), 500.0, "мл")
    stats.record_point(db, task, today, 620.0, "мл")
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["delta_from"] == today - timedelta(days=4)


def test_a_single_point_still_draws_a_line(db, member, task):
    """Ломаная из одной точки не рисуется вовсе — экран выглядел бы пустым."""
    _series(db, task, 500.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко", form="line")

    assert len(screens.screen_view(db, member.id, screen)["line"].split()) == 2


def test_a_short_series_says_how_many_days_it_has(db, member, task):
    """Неполное не выдаётся за полное: «данных за N дней из M» (ADR-0002)."""
    _series(db, task, 500.0, 620.0, 480.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert (view["days_have"], view["days_asked"]) == (3, screens.WINDOW_DAYS)
    assert view["short"]


def test_a_full_window_does_not_apologise_for_itself(db, member, task):
    _series(db, task, *[500.0] * screens.WINDOW_DAYS)
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    assert not screens.screen_view(db, member.id, screen)["short"]


def test_what_fell_out_of_the_window_is_not_shown(db, member, task):
    _series(db, task, *[500.0] * (screens.WINDOW_DAYS + 5))
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["days_have"] == screens.WINDOW_DAYS


def test_an_empty_series_is_an_honest_emptiness_and_not_a_zero(db, member, task):
    """Пустые сутки — не ноль: табло молчит, а не рисует провал, которого не было."""
    screen = screens.create_screen(db, member.id, task.id, "Молоко")

    view = screens.screen_view(db, member.id, screen)

    assert view["points"] == []
    assert view["last"] is None


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


def test_the_screen_opens_by_its_own_address(db, member, task, as_member):
    _series(db, task, 500.0, 620.0)
    screen = screens.create_screen(db, member.id, task.id, "Молоко за сутки")

    page = as_member.get(f"/stats/{screen.id}")

    assert page.status_code == 200
    assert "Молоко за сутки" in page.text
    assert "620" in page.text


def test_the_short_series_is_named_on_the_screen(db, member, task, as_member):
    _series(db, task, 500.0, 620.0)
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
