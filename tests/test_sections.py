"""Разделы знаний: личные рубрики, закрепление и полоса на экране «Знания».

Раздел — пользовательская рубрика знаний (тикет #25, спека #19): каждый заводит,
переименовывает и удаляет свои сам, чужих разделов не видит никто. Экран «Знания»
исключён из режима «от лица» (ADR-0005): переключение аватара его не меняет.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app
from app.modules.memory import knowledge
from app.modules.memory.models import Board, Section


# --- сервис: свои разделы и только свои -------------------------------------

def test_a_person_creates_renames_and_deletes_their_own_section(db, head):
    section = knowledge.create_section(db, head.id, "Ремонт")
    assert section.name == "Ремонт"

    knowledge.rename_section(db, head.id, section.id, "Ремонт дачи")
    assert knowledge.list_sections(db, head.id)[0].name == "Ремонт дачи"

    assert knowledge.delete_section(db, head.id, section.id)
    assert knowledge.list_sections(db, head.id) == []


def test_blank_name_does_not_make_a_section(db, head):
    assert knowledge.create_section(db, head.id, "   ") is None
    assert knowledge.list_sections(db, head.id) == []


def test_an_overlong_name_is_cut_to_the_column_limit(db, head):
    """SQLite длину String(128) не проверяет, боевой Postgres — падает."""
    section = knowledge.create_section(db, head.id, "х" * 300)
    assert len(section.name) == knowledge.NAME_LIMIT


def test_someone_elses_section_is_out_of_reach(db, head, member):
    section = knowledge.create_section(db, member.id, "Лёвино")

    assert knowledge.rename_section(db, head.id, section.id, "Захвачено") is None
    assert knowledge.toggle_pin(db, head.id, section.id) is None
    assert not knowledge.delete_section(db, head.id, section.id)

    survived = knowledge.list_sections(db, member.id)
    assert [s.name for s in survived] == ["Лёвино"]
    assert not survived[0].pinned


def test_pinned_sections_come_first_the_rest_by_freshness(db, head):
    stale = knowledge.create_section(db, head.id, "Старое")
    middle = knowledge.create_section(db, head.id, "Среднее")
    fresh = knowledge.create_section(db, head.id, "Свежее")
    now = datetime.utcnow()
    stale.last_activity_at = now - timedelta(days=7)
    middle.last_activity_at = now - timedelta(days=3)
    fresh.last_activity_at = now
    db.commit()

    knowledge.toggle_pin(db, head.id, stale.id)

    names = [s.name for s in knowledge.list_sections(db, head.id)]
    assert names == ["Старое", "Свежее", "Среднее"]


def test_deleting_a_section_takes_its_boards_with_it(db, head):
    """Без досок с активным доступом удаление — обычный каскад (блокировка — #28)."""
    section = knowledge.create_section(db, head.id, "Дети")
    db.add(Board(section_id=section.id, name="Питание и сон"))
    db.commit()

    assert knowledge.delete_section(db, head.id, section.id)
    assert db.query(Board).count() == 0


# --- экран «Знания» ----------------------------------------------------------

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


def test_the_screen_keeps_its_address_and_shows_the_strip(db, head, as_head):
    knowledge.create_section(db, head.id, "Здоровье")

    page = as_head.get("/memory")

    assert page.status_code == 200
    assert "Знания" in page.text
    assert "Здоровье" in page.text
    assert "Общее" in page.text


def test_knowledge_replaces_memory_in_the_navigation(db, head, as_head):
    page = as_head.get("/")

    assert 'href="/memory"' in page.text
    assert "Знания" in page.text
    assert "Память и заметки" not in page.text


def test_a_section_can_be_added_from_the_screen(db, head, as_head):
    response = as_head.post("/memory/sections/add", data={"name": "Машина"},
                            follow_redirects=False)

    assert response.status_code == 303
    assert [s.name for s in knowledge.list_sections(db, head.id)] == ["Машина"]


def test_rename_pin_and_delete_work_from_the_screen(db, head, as_head):
    section = knowledge.create_section(db, head.id, "Книги")

    as_head.post(f"/memory/sections/{section.id}/rename", data={"name": "Книги и фильмы"},
                 follow_redirects=False)
    as_head.post(f"/memory/sections/{section.id}/pin", follow_redirects=False)
    db.expire_all()
    assert (section.name, section.pinned) == ("Книги и фильмы", True)

    as_head.post(f"/memory/sections/{section.id}/delete", follow_redirects=False)
    assert knowledge.list_sections(db, head.id) == []


def test_the_active_section_is_marked_never_to_collapse(db, head, as_head):
    """Активный раздел не прячется в «⋯»: страница помечает его для app.js."""
    section = knowledge.create_section(db, head.id, "Путешествия")

    page = as_head.get(f"/memory?section={section.id}")

    assert "data-strip-active" in page.text


def test_a_garbage_section_parameter_falls_back_to_the_notes_view(db, head, as_head):
    """«²» проходит isdigit, но роняет int(); гигантское число не влезает в INTEGER."""
    for value in ["²", "abc", "99999999999999999999"]:
        assert as_head.get(f"/memory?section={value}").status_code == 200


def test_empty_common_explains_what_lands_there(db, head, as_head):
    page = as_head.get("/memory?section=common")

    assert "поделятся" in page.text


def test_the_screen_is_excluded_from_acting_as(db, head, member, as_head):
    """ADR-0005: глава семьи, переключив аватар, всё равно видит только своё."""
    knowledge.create_section(db, head.id, "Моё главы")
    knowledge.create_section(db, member.id, "Лёвино личное")

    as_head.post(f"/switch-member/{member.id}", follow_redirects=False)
    page = as_head.get("/memory")

    assert "Моё главы" in page.text
    assert "Лёвино личное" not in page.text


def test_someone_elses_section_cannot_be_opened_or_deleted_over_http(db, head, member, as_head):
    foreign = knowledge.create_section(db, member.id, "Лёвино личное")

    page = as_head.get(f"/memory?section={foreign.id}")
    assert "Лёвино личное" not in page.text

    as_head.post(f"/memory/sections/{foreign.id}/delete", follow_redirects=False)
    assert [s.name for s in knowledge.list_sections(db, member.id)] == ["Лёвино личное"]
