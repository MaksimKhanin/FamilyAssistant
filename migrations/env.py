"""Окружение Alembic: одна база всего ассистента.

Метаданные собираются так же, как при старте сервера: ядро плюс все модули.
Без `load_modules()` autogenerate не видел бы таблиц модулей и предлагал бы
их удалить.
"""
from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import settings
from app.core.db import Base
from app.modules import load_modules

load_modules()

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """`sqlalchemy.url` задают тесты; в остальных случаях — DATABASE_URL приложения."""
    return config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_offline() -> None:
    """Режим `--sql`: печатает DDL, к базе не подключается."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite не умеет ALTER на живых таблицах — будущие правки колонок
            # пойдут через пересоздание таблицы (batch mode). В Postgres режим
            # ничего не меняет.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
