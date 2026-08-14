"""Журнал питания на экране статистики: увидеть строки и убрать лишнее руками.

До этого удалить запись о еде можно было только через разговор («удали последнее»)
или с экрана «Приём пищи», и только за сегодня. Цифра за неделю при этом ничем не
правилась: человек видел лишние 900 ккал и не мог показать пальцем, какие именно.

Здесь проверяется вторая половина: строки под графиком и две кнопки — «убрать день»
и «убрать всё за период».
"""
import pytest
from fastapi.testclient import TestClient

from app.core.auth import ACTING_COOKIE, hash_password
from app.core.clock import local_today
from app.core.db import get_db
from app.main import app
from app.modules.nutrition import service
from app.modules.nutrition.vision import MealEstimate


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def as_head(client, db, head):
    head.password_hash = hash_password("pw")
    db.commit()
    client.post("/login", data={"username": head.username, "password": "pw"},
                follow_redirects=False)
    return client


def meal(db, user, **overrides):
    fields = dict(title="Овсянка", kcal=320, protein=12, fat=14, carbs=34)
    fields.update(overrides)
    return service.create_draft(db, user.id, MealEstimate(**fields))


def test_the_stats_screen_lists_what_the_chart_is_made_of(as_head, db, head):
    meal(db, head, title="Борщ со сметаной")
    service.log_activity(db, head.id, "walk", 30)

    markup = as_head.get("/nutrition/stats?period=week").text

    assert "Борщ со сметаной" in markup
    assert "Прогулка" in markup
    assert "/nutrition/stats/clear" in markup, "кнопки «убрать за период» на экране нет"


def test_a_cross_on_the_stats_screen_returns_to_the_stats_screen(as_head, db, head):
    """Крестик убирает запись и оставляет человека там же, где он нажал.

    Тот же обработчик отвечает и чату — репликой ассистента в ленту. Различает их
    `HX-Boosted`: переход в панели тоже идёт через htmx (ADR-0001), и по одному
    только `HX-Request` экран получал бы в ответ кусок переписки.
    """
    row = meal(db, head)

    response = as_head.post(f"/nutrition/meal/{row.id}/discard",
                            data={"back": "/nutrition/stats?period=week"},
                            headers={"HX-Request": "true", "HX-Boosted": "true"},
                            follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/nutrition/stats?period=week"
    assert service.get_meal(db, head.id, row.id) is None


def test_the_chat_still_gets_a_reply_and_not_a_redirect(as_head, db, head):
    row = meal(db, head)

    response = as_head.post(f"/nutrition/meal/{row.id}/discard",
                            headers={"HX-Request": "true"}, follow_redirects=False)

    assert response.status_code == 200
    assert "Удалил запись" in response.text


def test_a_foreign_address_is_not_a_way_back(as_head, db, head):
    row = meal(db, head)

    response = as_head.post(f"/nutrition/meal/{row.id}/discard",
                            data={"back": "https://example.com/"}, follow_redirects=False)

    assert response.headers["location"] == "/nutrition/meal"


def test_the_day_button_clears_exactly_that_day(as_head, db, head):
    from datetime import timedelta

    meal(db, head, title="Сегодняшнее")
    yesterday = meal(db, head, title="Вчерашнее", kcal=200)
    yesterday.eaten_at = yesterday.eaten_at - timedelta(days=1)
    db.commit()

    response = as_head.post(f"/nutrition/stats/day/{local_today().isoformat()}/clear",
                            data={"period": "week"}, follow_redirects=False)

    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert [m.title for m in service.records_for_period(db, head.id, "week")[0].meals] == ["Вчерашнее"]


def test_a_nonsense_day_changes_nothing(as_head, db, head):
    meal(db, head)

    response = as_head.post("/nutrition/stats/day/не-дата/clear",
                            data={"period": "week"}, follow_redirects=False)

    assert response.status_code == 303
    assert service.period_stats(db, head.id, "day").consumed == 320


def test_the_period_button_clears_both_halves_of_the_balance(as_head, db, head):
    meal(db, head)
    service.log_activity(db, head.id, "walk", 30)

    response = as_head.post("/nutrition/stats/clear", data={"period": "week"},
                            follow_redirects=False)

    assert response.status_code == 303
    stats = service.period_stats(db, head.id, "week")
    assert (stats.consumed, stats.burned) == (0, 0)


def test_nobody_clears_another_persons_days(client, db, head, member):
    """Чужие цифры и так не показываются — стереть их тем более нельзя.

    Домашний в шапке переключился на главу семьи: экран ему цифр не покажет
    (`can_see_figures`), и кнопка чистки должна молчать по той же причине.
    """
    member.password_hash = hash_password("pw")
    db.commit()
    client.post("/login", data={"username": member.username, "password": "pw"},
                follow_redirects=False)
    client.cookies.set(ACTING_COOKIE, str(head.id))
    meal(db, head)

    response = client.post("/nutrition/stats/clear", data={"period": "month"},
                           follow_redirects=False)

    assert response.status_code == 303
    assert service.period_stats(db, head.id, "day").consumed == 320
