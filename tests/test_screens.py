"""Каждый экран панели отдаётся целиком — и в обоих оформлениях.

Тест дешёвый и намеренно тупой: он не проверяет вёрстку, он проверяет, что
шаблон вообще собрался. Экраны делят один каркас, и переделка каркаса ломает
их все разом — а поймать это без такого прогона можно было только руками,
открыв семнадцать адресов подряд.
"""
import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app

#: Всё, что человек может открыть по ссылке из навигации, панели или уведомления.
SCREENS = [
    "/", "/chat", "/chat/panel", "/memory", "/reminders",
    "/settings/profile", "/settings/family", "/settings/connectors",
    "/settings/model", "/settings/traces",
    "/nutrition/meal", "/nutrition/stats", "/nutrition/activity", "/nutrition/plan",
    "/security/events", "/security/cameras", "/security/archive",
]


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_head(client, head):
    client.post("/login", data={"username": head.username, "password": "pw"},
                follow_redirects=False)
    return client


@pytest.mark.parametrize("path", SCREENS)
def test_every_screen_renders(as_head, path):
    response = as_head.get(path, follow_redirects=True)
    assert response.status_code == 200, (path, response.text[:400])


@pytest.mark.parametrize("path", SCREENS)
def test_every_screen_renders_in_the_dark_theme(as_head, path):
    """Ночное оформление — не переменная цвета, а второй набор токенов."""
    as_head.post("/settings/profile/theme", data={"theme": "dark"}, follow_redirects=False)

    response = as_head.get(path, follow_redirects=True)

    assert response.status_code == 200, (path, response.text[:400])
    if path != "/chat/panel":          # партиал панели живёт без <html>
        assert 'data-theme="dark"' in response.text


# --- оформление ---------------------------------------------------------------

def test_the_theme_is_saved_and_reaches_the_document(as_head, db, head):
    assert as_head.post("/settings/profile/theme", data={"theme": "dark"},
                        follow_redirects=False).status_code == 303

    db.refresh(head)
    assert head.theme == "dark"
    # Тема стоит атрибутом на <html>, а не подставляется скриптом: иначе ночью
    # первый кадр вспыхивал бы белым.
    assert 'data-theme="dark"' in as_head.get("/chat").text


def test_the_theme_form_reloads_the_document(as_head):
    """Форма оформления — единственная, которая ходит без hx-boost.

    Оформление живёт атрибутом на `<html>` и цветом статусбара в `<head>`, а
    переход подменяет только тело документа (ADR-0001): значение сохранялось,
    но цвета оставались прежними до перезагрузки, и кнопка выглядела нерабочей.
    Тест на сохранение темы этого не ловил — он ходит запросами, а не браузером.
    """
    markup = as_head.get("/settings/profile").text

    form = markup.split('action="/settings/profile/theme"')[1].split(">")[0]
    assert 'hx-boost="false"' in form


def test_an_unknown_theme_is_ignored(as_head, db, head):
    as_head.post("/settings/profile/theme", data={"theme": "neon"}, follow_redirects=False)

    db.refresh(head)
    assert head.theme == "warm"


def test_the_theme_is_personal(as_head, client, db, head, member):
    """Оформление своё у каждого — соседу по семье оно не достаётся."""
    as_head.post("/settings/profile/theme", data={"theme": "dark"}, follow_redirects=False)

    db.refresh(member)
    assert member.theme == "warm"


# --- нижняя панель ------------------------------------------------------------

def test_the_bottom_bar_is_home_talk_and_knowledge(as_head):
    """Три пункта, и разговор посередине: он есть всегда, даже без модулей."""
    markup = as_head.get("/").text

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


def test_the_talk_button_keeps_the_middle_when_a_module_is_off(as_head, db, head):
    """Выключенный модуль забирает свой пункт, но не место под ним."""
    from app.core.access import set_module_enabled

    set_module_enabled(db, head.id, "security", False)

    bar = as_head.get("/").text.split('<nav class="bottom-nav')[1].split("</nav>")[0]
    assert "/security/events" not in bar
    # Пустое место слева, разговор всё ещё в середине.
    assert bar.index("<span></span>") < bar.index('href="/chat"') < bar.index('href="/memory"')


# --- переезд «Агента и инструментов» -------------------------------------------

def test_the_agent_screen_moved_into_the_profile(as_head):
    response = as_head.get("/settings/agent", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/settings/profile"


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
def test_a_secondary_screen_leads_back_to_the_talk(as_head, path, title):
    """Разговор — главный экран, и со «Знаний» и «Профиля» возвращаются в него."""
    markup = as_head.get(path).text

    row = markup.split('class="screen-head-row"')[1].split("</div>")[0]
    assert 'href="/chat"' in row
    assert f'<div class="screen-title">{title}</div>' in markup


def test_the_profile_head_has_no_button_to_itself(as_head):
    head = (as_head.get("/settings/profile").text
            .split('class="screen-head-row"')[1].split('class="two-col"')[0])
    assert 'href="/settings/profile"' not in head


def test_the_sections_ride_along_with_the_head_of_the_knowledge_screen(as_head):
    """Полоса разделов стоит в шапке и липнет вместе с ней: раздел — это то,
    где ты находишься, и уезжать вверх он не должен."""
    markup = as_head.get("/memory").text

    assert markup.index('<div class="screen-head">') < markup.index('class="section-strip"')


def test_the_knowledge_head_keeps_the_profile_button(as_head):
    """Справа в шапке — тот же профиль, что и в разговоре."""
    row = as_head.get("/memory").text.split('class="screen-head-row"')[1]
    assert row.index('href="/settings/profile"') < row.index('class="section-strip"')


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


def test_the_profile_carries_the_agent_controls(as_head):
    markup = as_head.get("/settings/profile").text

    assert "Самостоятельность" in markup
    assert "/settings/agent/tools/log_meal" in markup
    assert "Что агент делал сегодня" in markup
