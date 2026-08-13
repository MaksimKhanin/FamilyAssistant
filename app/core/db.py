"""SQLAlchemy engine/session setup shared by core, agent layer and every module.

One database for the whole family; isolation between people is enforced at query
level by always scoping module tables on `user_id` (personal data) or `family_id`
(shared data such as cameras). See docs/module-contract.md.
"""
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

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


def create_all():
    """Deploy an empty database; a live one is left to Alembic.

    Module models must already be imported (app.modules.load_modules does that)
    or their tables will be missing from the metadata.

    На живой базе ничего не делает: схему меняет только `alembic upgrade head`
    (см. docs/deployment.md). Если бы create_all продолжал досоздавать таблицы
    на живой базе, каждый сервис, поднятый до прогона миграций, убегал бы вперёд
    них — и каждой будущей миграции пришлось бы защищаться от «таблица уже есть».
    """
    if "users" in inspect(engine).get_table_names():
        return
    Base.metadata.create_all(bind=engine)
    _stamp_migrations_head()


def _stamp_migrations_head():
    """Пометить свежесозданную базу головой миграций.

    База, только что созданная из моделей, по построению совпадает с головой:
    autogenerate-миграции — это дифф тех же моделей. Без штампа последующий
    `alembic upgrade head` начал бы с baseline и споткнулся о существующие таблицы.
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    with engine.begin() as connection:
        MigrationContext.configure(connection).stamp(script, script.get_current_head())
