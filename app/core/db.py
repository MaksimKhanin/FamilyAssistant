"""SQLAlchemy engine/session setup shared by core, agent layer and every module.

One database for the whole family; isolation between people is enforced at query
level by always scoping module tables on `user_id` (personal data) or `family_id`
(shared data such as cameras). See docs/module-contract.md.
"""
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

# Произвольная константа, общая для всех процессов: под ней в Postgres берётся
# advisory-блокировка на время прогона миграций (см. `_schema_lock`).
_SCHEMA_LOCK_KEY = 4872003115

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True, future=True)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _):
    """SQLite по умолчанию не соблюдает внешние ключи.

    Без этого `ON DELETE CASCADE` работает в Postgres и молча не работает
    локально: удалённый человек оставлял бы за собой записи о еде и свои разделы.
    Пусть локальная база ведёт себя как боевая.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

Base = declarative_base()


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    """Standalone session for background workers (bot, scheduler, event handlers)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upgrade_schema():
    """Довести схему до головы миграций — тем же `alembic upgrade head`.

    Вызывается при старте каждым процессом (веб, планировщик, бот), и это
    единственный способ, которым схема появляется и меняется: пустую базу
    разворачивает baseline-миграция, отставшую догоняют следующие за ней.
    Схема едет вместе с кодом, а не отдельной командой руками: пропущенная
    команда означала процесс, который уже умеет пользоваться новой таблицей,
    и базу, в которой её ещё нет, — то есть `relation ... does not exist`
    в логе базы вместо работающей функции.

    Импортированные модели здесь не нужны: таблицы заводит миграция, а не
    метаданные (`migrations/env.py` сам грузит модули — ради autogenerate).
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with _schema_lock():
        command.upgrade(config, "head")


@contextmanager
def _schema_lock():
    """Миграции накатывает ровно один процесс, остальные ждут его на старте.

    Веб, планировщик и бот поднимаются одновременно, и без блокировки два
    процесса начали бы создавать одни и те же таблицы наперегонки: кто пришёл
    вторым, тот упал бы на «таблица уже есть». В SQLite (локальный запуск и
    тесты) процесс всегда один — блокировать нечего.
    """
    if settings.database_url.startswith("sqlite"):
        yield
        return

    with engine.connect() as connection:
        connection.exec_driver_sql(f"SELECT pg_advisory_lock({_SCHEMA_LOCK_KEY})")
        try:
            yield
        finally:
            connection.exec_driver_sql(f"SELECT pg_advisory_unlock({_SCHEMA_LOCK_KEY})")
