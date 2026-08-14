"""Переезд заметок на доски и в напоминания (тикет #33, спека #19).

Тест не заглядывает в файл миграции: он засевает базу, дожившую до переезда,
заметками всех четырёх видов и запускает `alembic upgrade member` — ровно то, что
запустит эксплуатирующий, — а потом смотрит на результат глазами обычных запросов.
"""
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]

#: Ревизия, на которой заметки ещё лежат в своей плоской таблице.
BEFORE_THE_MOVE = "0007"

#: Заметки семьи накануне переезда: все четыре вида, включая живой срок,
#: расплывчатый срок и пустую заметку.
NOTES = [
    # kind,     text,                        source,                pinned, when_text,        remind_at,             reminded_at
    ("pref",   "Соня не ест грибы",         "из разговора 3 августа", 0,    None,             None,                  None),
    ("health", "У Лёвы аллергия на арахис", "добавлено вручную",      1,    None,             None,                  None),
    ("fact",   "Летом едем к бабушке",      "из разговора",           0,    None,             None,                  None),
    ("task",   "позвонить врачу",           "из разговора",           0,    "завтра в 9",     "2026-08-14 09:00:00", "2026-08-14 09:01:00"),
    ("task",   "купить корм коту",          "добавлено вручную",      0,    "в пятницу утром", None,                 None),
    ("fact",   "   ",                       "из разговора",           0,    None,             None,                  None),
]


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def lived_in(tmp_path):
    """База, дожившая до переезда: семья, человек и его заметки."""
    url = f"sqlite:///{tmp_path}/lived.db"
    command.upgrade(_config(url), BEFORE_THE_MOVE)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO families (id, name, created_at) "
                             "VALUES (1, 'Семья', '2026-01-01 00:00:00')"))
        conn.execute(sa.text(
            "INSERT INTO users (id, family_id, username, display_name, role, avatar_slot, "
            "                   autonomy, created_at) "
            "VALUES (1, 1, 'marina', 'Марина', 'member', 0, 1, '2026-01-01 00:00:00')"))
        for number, note in enumerate(NOTES):
            kind, text, source, pinned, when_text, remind_at, reminded_at = note
            conn.execute(sa.text(
                "INSERT INTO notes (user_id, text, kind, source, pinned, when_text, remind_at, "
                "                   reminded_at, created_at) "
                "VALUES (1, :text, :kind, :source, :pinned, :when_text, :remind_at, "
                "        :reminded_at, :created_at)"),
                {"text": text, "kind": kind, "source": source, "pinned": pinned,
                 "when_text": when_text, "remind_at": remind_at, "reminded_at": reminded_at,
                 "created_at": f"2026-08-0{number + 1} 12:00:00"})
    engine.dispose()
    command.upgrade(_config(url), "head")
    return sa.create_engine(url)


def _rows(engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).mappings().all()


def _entries(engine, board: str):
    return _rows(engine,
                 "SELECT e.text, e.author_id, e.by_assistant, e.created_at "
                 "FROM board_entries e JOIN boards b ON b.id = e.board_id "
                 "WHERE b.name = :board ORDER BY e.id", board=board)


def test_every_kind_of_note_lands_on_its_own_board_in_the_personal_section(lived_in):
    boards = _rows(lived_in,
                   "SELECT b.name FROM boards b JOIN sections s ON s.id = b.section_id "
                   "WHERE s.user_id = 1 AND s.name = 'Личное' ORDER BY b.name")

    assert [row["name"] for row in boards] == ["Здоровье", "Наблюдения", "Предпочтения"]


def test_the_author_is_read_out_of_the_old_source_field(lived_in):
    """«Добавлено вручную» писал человек, «из разговора…» — ассистент (ADR-0004)."""
    by_assistant = _entries(lived_in, "Предпочтения")[0]
    by_hand = _entries(lived_in, "Здоровье")[0]

    assert (by_assistant["author_id"], by_assistant["by_assistant"]) == (None, 1)
    assert (by_hand["author_id"], by_hand["by_assistant"]) == (1, 0)


def test_the_entry_keeps_the_words_and_the_time_of_the_note(lived_in):
    entry = _entries(lived_in, "Предпочтения")[0]

    assert entry["text"] == "Соня не ест грибы"
    assert entry["created_at"].startswith("2026-08-01")


def test_a_note_with_a_live_deadline_becomes_a_reminder_that_remembers_its_firing(lived_in):
    reminders = _rows(lived_in, "SELECT text, remind_at, reminded_at FROM reminders")

    assert len(reminders) == 1
    assert reminders[0]["text"] == "позвонить врачу"
    assert reminders[0]["remind_at"].startswith("2026-08-14 09:00")
    assert reminders[0]["reminded_at"].startswith("2026-08-14 09:01")


def test_a_vague_deadline_becomes_an_entry_and_not_an_invented_reminder(lived_in):
    """«В пятницу утром» — не момент: выдумывать за человека пятницу нельзя."""
    texts = [row["text"] for row in _entries(lived_in, "Наблюдения")]

    assert "купить корм коту (когда-то: в пятницу утром)" in texts
    assert [row["text"] for row in _rows(lived_in, "SELECT text FROM reminders")] \
        == ["позвонить врачу"]


def test_an_empty_note_does_not_become_an_empty_entry(lived_in):
    texts = [row["text"] for row in _entries(lived_in, "Наблюдения")]

    assert texts == ["Летом едем к бабушке", "купить корм коту (когда-то: в пятницу утром)"]


def test_the_board_wears_the_freshness_of_its_last_entry(lived_in):
    """По свежести сортируются и доски, и полоса разделов — она не должна
    оказаться в дне переезда."""
    fresh = _rows(lived_in,
                  "SELECT b.last_activity_at AS board, s.last_activity_at AS section "
                  "FROM boards b JOIN sections s ON s.id = b.section_id "
                  "WHERE b.name = 'Наблюдения'")[0]

    assert fresh["board"].startswith("2026-08-05")
    assert fresh["section"].startswith("2026-08-05")


def test_the_old_table_is_kept_under_its_legacy_name(lived_in):
    """Перенос разбирает свободный текст — человеку должно остаться куда посмотреть."""
    tables = set(sa.inspect(lived_in).get_table_names())
    assert "notes_legacy" in tables
    assert "notes" not in tables

    kept = _rows(lived_in, "SELECT id FROM notes_legacy")
    assert len(kept) == len(NOTES)


def test_the_move_runs_once_and_the_second_upgrade_is_silent(lived_in, tmp_path):
    command.upgrade(_config(f"sqlite:///{tmp_path}/lived.db"), "head")

    assert len(_rows(lived_in, "SELECT id FROM board_entries")) == 4
    assert len(_rows(lived_in, "SELECT id FROM reminders")) == 1


def test_a_living_section_is_not_dragged_back_by_old_notes(tmp_path):
    """У «Личного» уже могла быть свежая запись: переезд старого её не отменяет."""
    url = f"sqlite:///{tmp_path}/fresh-section.db"
    engine = _seeded(url, section_activity="2026-09-01 00:00:00")

    fresh = _rows(engine, "SELECT last_activity_at FROM sections WHERE user_id = 1")[0]

    assert fresh["last_activity_at"].startswith("2026-09-01")


def test_the_personal_section_is_not_doubled_when_it_already_exists(tmp_path):
    """«Личное» мог завести ассистент под свою доску — переезд встаёт рядом."""
    engine = _seeded(f"sqlite:///{tmp_path}/with-section.db")

    sections = _rows(engine, "SELECT id FROM sections WHERE user_id = 1 AND name = 'Личное'")

    assert len(sections) == 1
    assert len(_entries(engine, "Предпочтения")) == 1


def _seeded(url: str, section_activity: str = "2026-02-01 00:00:00"):
    """База, у которой раздел «Личное» уже был к переезду, и одна старая заметка."""
    command.upgrade(_config(url), BEFORE_THE_MOVE)
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO families (id, name, created_at) "
                             "VALUES (1, 'Семья', '2026-01-01 00:00:00')"))
        conn.execute(sa.text(
            "INSERT INTO users (id, family_id, username, display_name, role, avatar_slot, "
            "                   autonomy, created_at) "
            "VALUES (1, 1, 'marina', 'Марина', 'member', 0, 1, '2026-01-01 00:00:00')"))
        conn.execute(sa.text(
            "INSERT INTO sections (user_id, name, pinned, last_activity_at, created_at) "
            "VALUES (1, 'Личное', 0, :active, '2026-02-01 00:00:00')"), {"active": section_activity})
        conn.execute(sa.text(
            "INSERT INTO notes (user_id, text, kind, source, pinned, created_at) "
            "VALUES (1, 'Соня не ест грибы', 'pref', 'из разговора', 0, '2026-08-01 12:00:00')"))
    engine.dispose()

    command.upgrade(_config(url), "head")
    return sa.create_engine(url)
