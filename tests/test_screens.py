"""Каждый экран панели отдаётся целиком — и в обоих оформлениях.

Тест дешёвый и намеренно тупой: он не проверяет вёрстку, он проверяет, что
шаблон вообще собрался. Экраны делят один каркас, и переделка каркаса ломает
их все разом — а поймать это без такого прогона можно было только руками,
открыв семнадцать адресов подряд.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.clock import utc_now
from app.core.db import get_db
from app.core.models import ChatMessage
from app.main import app

#: Всё, что участник может открыть по ссылке из навигации, панели или уведомления.
MEMBER_SCREENS = [
    "/", "/chat", "/chat/panel", "/memory", "/reminders",
    "/settings/profile", "/settings/family", "/settings/connectors",
    "/nutrition/meal", "/nutrition/stats", "/nutrition/activity", "/nutrition/plan",
    "/security/events", "/security/archive",
]

#: Админ-раздел: настройки на всю семью, люди, трейсы и настройка камер.
ADMIN_SCREENS = [
    "/settings/accounts", "/settings/agent", "/settings/model", "/settings/traces",
    "/settings/profile", "/onboarding", "/security/cameras",
]


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
def as_admin(client, admin):
    client.post("/login", data={"username": admin.username, "password": "pw"},
                follow_redirects=False)
    return client


@pytest.mark.parametrize("path", MEMBER_SCREENS)
def test_every_screen_renders(as_member, path):
    response = as_member.get(path, follow_redirects=True)
    assert response.status_code == 200, (path, response.text[:400])


@pytest.mark.parametrize("path", ADMIN_SCREENS)
def test_every_admin_screen_renders(as_admin, path):
    response = as_admin.get(path, follow_redirects=True)
    assert response.status_code == 200, (path, response.text[:400])


@pytest.mark.parametrize("path", MEMBER_SCREENS)
def test_every_screen_renders_in_the_dark_theme(as_member, path):
    """Ночное оформление — не переменная цвета, а второй набор токенов."""
    as_member.post("/settings/profile/theme", data={"theme": "dark"}, follow_redirects=False)

    response = as_member.get(path, follow_redirects=True)

    assert response.status_code == 200, (path, response.text[:400])
    if path != "/chat/panel":          # партиал панели живёт без <html>
        assert 'data-theme="dark"' in response.text


# --- оформление ---------------------------------------------------------------

def test_the_theme_is_saved_and_reaches_the_document(as_member, db, member):
    assert as_member.post("/settings/profile/theme", data={"theme": "dark"},
                        follow_redirects=False).status_code == 303

    db.refresh(member)
    assert member.theme == "dark"
    # Тема стоит атрибутом на <html>, а не подставляется скриптом: иначе ночью
    # первый кадр вспыхивал бы белым.
    assert 'data-theme="dark"' in as_member.get("/chat").text


def test_the_theme_form_reloads_the_document(as_member):
    """Форма оформления — единственная, которая ходит без hx-boost.

    Оформление живёт атрибутом на `<html>` и цветом статусбара в `<head>`, а
    переход подменяет только тело документа (ADR-0001): значение сохранялось,
    но цвета оставались прежними до перезагрузки, и кнопка выглядела нерабочей.
    Тест на сохранение темы этого не ловил — он ходит запросами, а не браузером.
    """
    markup = as_member.get("/settings/profile").text

    form = markup.split('action="/settings/profile/theme"')[1].split(">")[0]
    assert 'hx-boost="false"' in form


def test_an_unknown_theme_is_ignored(as_member, db, member):
    as_member.post("/settings/profile/theme", data={"theme": "neon"}, follow_redirects=False)

    db.refresh(member)
    assert member.theme == "warm"


def test_the_theme_is_personal(as_member, client, db, member, other):
    """Оформление своё у каждого — соседу по семье оно не достаётся."""
    as_member.post("/settings/profile/theme", data={"theme": "dark"}, follow_redirects=False)

    db.refresh(other)
    assert other.theme == "warm"


# --- нижняя панель ------------------------------------------------------------

def test_the_bottom_bar_is_home_talk_and_knowledge(as_member):
    """Три пункта, и разговор посередине: он есть всегда, даже без модулей."""
    markup = as_member.get("/").text

    bar = markup.split('<nav class="bottom-nav')[1].split("</nav>")[0]
    assert bar.count("<a ") == 3
    assert "/security/events" in bar and "/memory" in bar
    assert 'href="/chat"' in bar
    # Внизу пункт зовётся «Дом», хотя в сайдбаре компьютера он «События»:
    # туда идут посмотреть, что дома, а не за списком событий.
    assert "<span>Дом</span>" in bar and "<span>События</span>" not in bar
    # Ссылок на «Главную» и «Записать еду» в панели больше нет: о еде проще
    # сказать словами, а обзор дня переехал в шапку разговора.
    assert 'href="/nutrition/meal"' not in bar


def test_the_talk_button_keeps_the_middle_when_a_module_is_off(as_member, db, member):
    """Выключенный модуль забирает свой пункт, но не место под ним."""
    from app.core.access import set_module_enabled

    set_module_enabled(db, member.id, "security", False)

    bar = as_member.get("/").text.split('<nav class="bottom-nav')[1].split("</nav>")[0]
    assert "/security/events" not in bar
    # Пустое место слева, разговор всё ещё в середине.
    assert bar.index("<span></span>") < bar.index('href="/chat"') < bar.index('href="/memory"')


# --- разделение ролей ----------------------------------------------------------

def test_the_admin_area_is_closed_to_a_family_member(as_member):
    """Спрятать пункт мало: адрес, набранный руками, тоже не должен открыться."""
    for path in ("/settings/accounts", "/settings/agent", "/settings/model",
                 "/settings/traces", "/security/cameras", "/onboarding"):
        assert as_member.get(path).status_code == 403, path


def test_the_members_screens_are_closed_to_the_administrator(as_admin):
    """У администратора нет ни разговора, ни модулей — только настройки."""
    for path in ("/chat", "/memory", "/reminders", "/nutrition/meal", "/security/events"):
        assert as_admin.get(path).status_code == 403, path


def test_the_administrator_lands_in_the_admin_area(as_admin):
    """«Главная» — экран участника, и админа с неё уводит к его собственной работе."""
    response = as_admin.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings/accounts"


def test_the_administrator_has_no_talk_in_the_shell(as_admin):
    markup = as_admin.get("/settings/accounts").text

    assert 'href="/chat"' not in markup
    assert 'class="bottom-nav' not in markup
    assert "Администратор" in markup


def test_the_member_navigation_carries_no_admin_items(as_member):
    markup = as_member.get("/").text

    assert "Администрирование" not in markup
    assert 'href="/settings/traces"' not in markup
    assert 'href="/settings/accounts"' not in markup


# --- карточки ассистента --------------------------------------------------------

#: По одной карточке каждого вида. Половина из них появляется в переписке редко —
#: карточка события с камеры ждёт ночной аномалии, — и до сих пор их вёрстку
#: можно было проверить только дождавшись подходящего разговора.
CARDS = [
    {"type": "meal", "meal_id": 1, "title": "Суп и салат", "kcal": 420, "protein": 18,
     "fat": 14, "carbs": 48, "is_estimate": True},
    {"type": "stats", "consumed": 1250, "burned": 420, "period": "day"},
    {"type": "security", "event_id": 7, "title": "Кто-то у калитки", "camera": "Калитка",
     "verdict": "check", "at": "23:14"},
    {"type": "plan", "days": [{"title": "Сегодня", "kcal": 2100,
                               "meals": [{"name": "омлет"}]}], "comment": "Без рыбы"},
    {"type": "dish", "title": "Салат из огурцов с фетой", "slot": "ужин", "kcal": 320,
     "protein": 12, "fat": 22, "carbs": 14, "portion": "тарелка ~280 г",
     "why": "Лёгкий ужин из того, что есть дома"},
    {"type": "recipe", "title": "Салат из огурцов с фетой", "portions": 2, "kcal": 320,
     "protein": 12, "fat": 22, "carbs": 14, "ingredients": ["огурцы — 400 г"],
     "steps": ["Нарезать огурцы.", "Заправить маслом."], "note": ""},
    {"type": "board", "board": "Память ассистента", "text": "Соня не ест грибы", "url": "/memory"},
    {"type": "stats-screen", "board": "Кормления", "name": "Молоко за день",
     "form": "столбики", "url": "/stats/1"},
    {"type": "board-recall", "hits": [{"board": "Кормления", "text": "9.10 150"}]},
    {"type": "reminder", "text": "забрать посылку", "when": "завтра в 9:00"},
    {"type": "confirm", "pending_id": 3, "title": "Удалить запись",
     "tool": "discard_meal", "arguments": {"meal_id": 1}},
]


@pytest.mark.parametrize("card", CARDS, ids=lambda c: c["type"])
def test_every_agent_card_renders(card):
    from app.core.templating import templates

    markup = templates.get_template("partials/chat_messages.html").render(
        messages=[{"role": "assistant", "text": "Готово.", "traces": [], "cards": [card]}],
    )

    assert "agent-card" in markup, card["type"]


# --- шапка вторичного экрана ----------------------------------------------------

@pytest.mark.parametrize("path,title", [("/memory", "Знания"),
                                        ("/settings/profile", "Профиль и агент")])
def test_a_secondary_screen_leads_back_to_the_talk(as_member, path, title):
    """Разговор — главный экран, и со «Знаний» и «Профиля» возвращаются в него."""
    markup = as_member.get(path).text

    row = markup.split('class="screen-head-row"')[1].split("</div>")[0]
    assert 'href="/chat"' in row
    assert f'<div class="screen-title">{title}</div>' in markup


def test_the_profile_head_has_no_button_to_itself(as_member):
    head = (as_member.get("/settings/profile").text
            .split('class="screen-head-row"')[1].split('class="two-col"')[0])
    assert 'href="/settings/profile"' not in head


def test_the_sections_ride_along_with_the_head_of_the_knowledge_screen(as_member):
    """Полоса разделов стоит в шапке и липнет вместе с ней: раздел — это то,
    где ты находишься, и уезжать вверх он не должен."""
    markup = as_member.get("/memory").text

    assert markup.index('<div class="screen-head">') < markup.index('class="section-strip"')


def test_the_knowledge_head_keeps_the_profile_button(as_member):
    """Справа в шапке — тот же профиль, что и в разговоре."""
    row = as_member.get("/memory").text.split('class="screen-head-row"')[1]
    assert row.index('href="/settings/profile"') < row.index('class="section-strip"')


# --- меню достижимо с любого экрана ---------------------------------------------

#: Экраны, у которых на телефоне своя шапка вместо шапки каркаса: та скрыта
#: (.screen-chat .header, .screen-focus .header), и кнопку меню каждый такой
#: экран обязан показать сам — иначе с него не попасть никуда, кроме «назад».
OWN_HEAD_SCREENS = ["/chat", "/memory", "/settings/profile"]


@pytest.mark.parametrize("path", OWN_HEAD_SCREENS)
def test_a_screen_with_its_own_head_still_opens_the_drawer(as_member, path):
    assert "panel.openDrawer()" in as_member.get(path).text, path


@pytest.mark.parametrize("path", MEMBER_SCREENS)
def test_every_member_screen_can_reach_the_menu(as_member, path):
    """На телефоне выдвижное меню — единственный путь к разделам, которых нет
    в нижней панели. Кнопка, которая его открывает, должна быть на каждом
    экране: либо в шапке каркаса, либо в собственной шапке экрана."""
    if path == "/chat/panel":         # партиал панели, а не экран
        return
    assert "panel.openDrawer()" in as_member.get(path, follow_redirects=True).text, path


# --- выход из учётной записи ------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/chat", "/settings/profile"])
def test_the_menu_offers_a_way_out(as_member, path):
    """Войти под другой учётной записью нельзя, не выйдя из этой."""
    assert 'action="/logout"' in as_member.get(path).text, path


def test_the_admin_can_log_out_too(as_admin):
    assert 'action="/logout"' in as_admin.get("/settings/accounts").text


def test_logging_out_drops_the_session(as_member):
    assert as_member.post("/logout", follow_redirects=False).status_code == 303

    assert as_member.get("/", follow_redirects=False).headers["location"] == "/login"


# --- разделители дня в разговоре ------------------------------------------------

def test_the_history_is_split_by_days():
    """Разговор длится месяцами: без даты вчерашний ответ читается как сегодняшний."""
    from datetime import timedelta

    from app.core.clock import utc_now
    from app.web.routes_chat import _with_day_separators

    now = utc_now()
    messages = _with_day_separators([
        {"role": "user", "text": "вчера", "at": now - timedelta(days=1)},
        {"role": "assistant", "text": "и тоже вчера", "at": now - timedelta(days=1)},
        {"role": "user", "text": "сегодня", "at": now},
    ])

    assert messages[0]["daysep"] == "Вчера"
    assert "daysep" not in messages[1]        # второй раз за тот же день не надо
    assert messages[2]["daysep"] == "Сегодня"


def test_an_answer_arriving_now_carries_no_day_separator():
    """Ответ прилетает в конец ленты по HTMX — «Сегодня» посреди переписки сбивает."""
    from app.core.templating import templates

    markup = templates.get_template("partials/chat_messages.html").render(
        messages=[{"role": "assistant", "text": "Готово.", "traces": [], "cards": []}],
    )

    assert "chat-daysep" not in markup


def test_the_profile_shows_the_family_dials_without_the_handles(as_member):
    """Самостоятельность видно, но крутит её администратор — на всю семью."""
    markup = as_member.get("/settings/profile").text

    assert "Самостоятельность" in markup
    assert "Что агент делал сегодня" in markup
    assert "/settings/agent/tools/log_meal" not in markup
    assert 'action="/settings/agent/autonomy"' not in markup


def test_the_admin_profile_is_only_the_password_and_the_theme(as_admin):
    markup = as_admin.get("/settings/profile").text

    assert 'action="/settings/profile/theme"' in markup
    assert 'action="/settings/profile/password"' in markup
    assert "Характер ассистента" not in markup
    assert "Что агент делал сегодня" not in markup


def test_the_agent_screen_sets_the_dials_for_everyone(as_admin):
    markup = as_admin.get("/settings/agent").text

    assert "Самостоятельность" in markup
    assert "/settings/agent/tools/log_meal" in markup
    assert "одинаково для всех" in markup


def _message(db, user, days_ago=0):
    row = ChatMessage(user_id=user.id, role="user", content="Привет",
                      created_at=utc_now() - timedelta(days=days_ago))
    db.add(row)
    db.commit()
    return row


def test_clearing_the_whole_history_removes_every_message(as_member, db, member, other):
    _message(db, member, days_ago=0)
    _message(db, member, days_ago=40)
    _message(db, other, days_ago=0)

    response = as_member.post("/settings/profile/chat/clear", data={"period": "all"},
                              follow_redirects=False)

    assert response.status_code == 303
    assert db.query(ChatMessage).filter(ChatMessage.user_id == member.id).count() == 0
    # Чужая переписка не тронута: история — личные данные, а не общая на семью.
    assert db.query(ChatMessage).filter(ChatMessage.user_id == other.id).count() == 1


def test_clearing_by_period_keeps_older_messages(as_member, db, member):
    _message(db, member, days_ago=0)
    _message(db, member, days_ago=40)

    as_member.post("/settings/profile/chat/clear", data={"period": "week"}, follow_redirects=False)

    remaining = db.query(ChatMessage).filter(ChatMessage.user_id == member.id).all()
    assert len(remaining) == 1
    assert remaining[0].created_at < utc_now() - timedelta(days=30)


def test_clearing_the_history_shows_a_notice_with_the_count(as_member, db, member):
    _message(db, member, days_ago=0)
    _message(db, member, days_ago=0)

    response = as_member.post("/settings/profile/chat/clear", data={"period": "all"},
                              follow_redirects=True)

    assert "2 сообщения" in response.text


def test_an_admin_cannot_reach_the_clear_history_route(as_admin):
    """У администратора нет разговора (ADR-0008), значит и стирать ему нечего."""
    response = as_admin.post("/settings/profile/chat/clear", data={"period": "all"},
                             follow_redirects=False)

    assert response.status_code == 403
