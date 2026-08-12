"""Прогон Alembic как внешнее поведение: команда `upgrade head` и итоговая схема.

Тесты не заглядывают в файлы миграций — они запускают то же самое, что запустит
эксплуатирующий, и смотрят на базу глазами инспектора SQLAlchemy.
"""
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def _upgrade_head(db_url: str):
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def test_upgrade_on_an_empty_database_creates_the_knowledge_tables(tmp_path):
    """`alembic upgrade head` на пустой базе заводит разделы, доски, записи и доступ."""
    url = f"sqlite:///{tmp_path}/fresh.db"
    _upgrade_head(url)

    insp = sa.inspect(sa.create_engine(url))
    tables = set(insp.get_table_names())
    assert {"sections", "boards", "board_entries", "board_shares"} <= tables

    sections = {c["name"] for c in insp.get_columns("sections")}
    assert {"id", "user_id", "name", "pinned", "last_activity_at", "created_at"} <= sections

    boards = {c["name"] for c in insp.get_columns("boards")}
    assert {"id", "section_id", "name", "instruction", "last_activity_at", "created_at"} <= boards
    # Владелец доски не дублируется: вычисляется через раздел.
    assert "user_id" not in boards

    entries = {c["name"] for c in insp.get_columns("board_entries")}
    assert {"id", "board_id", "author_id", "by_assistant", "text", "created_at", "edited_at"} <= entries

    shares = {c["name"] for c in insp.get_columns("board_shares")}
    assert {"id", "board_id", "user_id", "right"} <= shares


def _schema_snapshot(url: str):
    """Схема глазами инспектора: таблицы, колонки, внешние ключи, уникальности."""
    insp = sa.inspect(sa.create_engine(url))
    snapshot = {}
    for table in insp.get_table_names():
        snapshot[table] = {
            "columns": {c["name"]: (str(c["type"]), c["nullable"]) for c in insp.get_columns(table)},
            "fks": sorted(
                (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
                for fk in insp.get_foreign_keys(table)
            ),
            "uniques": sorted(tuple(u["column_names"]) for u in insp.get_unique_constraints(table)),
            "indexes": sorted(
                (tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(table)
            ),
        }
    return snapshot


def test_upgrade_on_a_database_born_from_create_all_converges_to_the_same_schema(tmp_path):
    """`alembic upgrade head` на живой базе, созданной `create_all()`, ничего не ломает.

    Обе дороги — миграции с нуля и `create_all()` + миграции — обязаны приводить
    к одному и тому же состоянию схемы: это и есть контракт baseline-миграции.
    """
    from app.core.db import Base

    fresh_url = f"sqlite:///{tmp_path}/fresh.db"
    _upgrade_head(fresh_url)

    lived_url = f"sqlite:///{tmp_path}/lived.db"
    engine = sa.create_engine(lived_url)
    Base.metadata.create_all(bind=engine)   # так базу создаёт сервер при старте
    engine.dispose()
    _upgrade_head(lived_url)

    assert _schema_snapshot(lived_url) == _schema_snapshot(fresh_url)


def test_running_the_upgrade_twice_changes_nothing(tmp_path):
    """Повторный `upgrade head` — тихий no-op, а не ошибка «таблица уже есть»."""
    url = f"sqlite:///{tmp_path}/fresh.db"
    _upgrade_head(url)
    before = _schema_snapshot(url)
    _upgrade_head(url)
    assert _schema_snapshot(url) == before


def test_create_all_deploys_an_empty_database_already_stamped_at_head(db):
    """Пустая база от `create_all()` сразу помечена головой миграций.

    Без штампа первый же `alembic upgrade head` начал бы с baseline и в будущих
    миграциях спотыкался бы о таблицы, которые `create_all()` создал из моделей.
    """
    from app.core.config import settings
    from app.core.db import Base, create_all, engine

    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")

    create_all()

    insp = sa.inspect(engine)
    assert "sections" in insp.get_table_names()
    with engine.connect() as conn:
        stamped = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert stamped is not None
    _upgrade_head(settings.database_url)   # и после штампа это тихий no-op


def test_create_all_leaves_a_lived_in_database_to_migrations(db, head):
    """Живую базу `create_all()` не трогает — недостающее довозит только Alembic.

    Иначе сервис, поднятый до прогона миграций, убегал бы вперёд них, и каждой
    будущей миграции пришлось бы защищаться от «таблица уже есть».
    """
    from app.core.config import settings
    from app.core.db import create_all, engine

    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        for table in ("board_shares", "board_entries", "boards", "sections"):
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")

    create_all()
    assert "sections" not in sa.inspect(engine).get_table_names()

    _upgrade_head(settings.database_url)
    assert "sections" in sa.inspect(engine).get_table_names()


def test_upgrade_creates_the_reminders_table_outside_the_knowledge_tables(tmp_path):
    """Напоминания — своя таблица вне знаний: каскад от участника, без ссылок на доски."""
    url = f"sqlite:///{tmp_path}/fresh.db"
    _upgrade_head(url)

    insp = sa.inspect(sa.create_engine(url))
    assert "reminders" in insp.get_table_names()

    columns = {c["name"] for c in insp.get_columns("reminders")}
    assert {"id", "user_id", "text", "remind_at", "reminded_at", "created_at"} <= columns

    fks = insp.get_foreign_keys("reminders")
    assert len(fks) == 1
    assert fks[0]["referred_table"] == "users"
    assert fks[0]["options"].get("ondelete", "").upper() == "CASCADE"


def test_entry_author_survives_his_own_deletion_but_the_board_does_not(tmp_path):
    """Внешние ключи по ADR-0004: автор записи — SET NULL, доска — CASCADE."""
    url = f"sqlite:///{tmp_path}/fresh.db"
    _upgrade_head(url)

    insp = sa.inspect(sa.create_engine(url))
    fks = {fk["constrained_columns"][0]: fk for fk in insp.get_foreign_keys("board_entries")}

    author = fks["author_id"]
    assert author["referred_table"] == "users"
    assert author["options"].get("ondelete", "").upper() == "SET NULL"

    board = fks["board_id"]
    assert board["referred_table"] == "boards"
    assert board["options"].get("ondelete", "").upper() == "CASCADE"
