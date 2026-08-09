"""SQLAlchemy engine/session setup shared by core, agent layer and every module.

One database for the whole family; isolation between people is enforced at query
level by always scoping module tables on `user_id` (personal data) or `family_id`
(shared data such as cameras). See docs/module-contract.md.
"""
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True, future=True)
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
    """Create tables for everything imported so far.

    Module models must already be imported (app.modules.load_modules does that)
    or their tables will be missing from the metadata.
    """
    Base.metadata.create_all(bind=engine)
