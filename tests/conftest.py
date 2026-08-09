"""Test fixtures.

Environment is set before any app import, because settings are read once at import
time. Each test gets its own SQLite file and media directory, so nothing leaks
between tests and nothing touches a real database.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="family-assistant-tests-")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("INGEST_API_KEY", "test-ingest-key")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("MEDIA_ROOT", f"{_TMP}/media")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:0/v1")
os.environ.setdefault("LLM_MODEL", "test-model")

import pytest  # noqa: E402

from app.core.auth import hash_password  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core.family import get_settings  # noqa: E402
from app.core.models import Family, ROLE_HEAD, ROLE_MEMBER, User  # noqa: E402
from app.modules import load_modules  # noqa: E402

load_modules()   # регистрирует таблицы модулей и инструменты агента


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def family(db):
    row = Family(name="Тестовая семья")
    db.add(row)
    db.flush()
    get_settings(db, row.id)
    db.commit()
    return row


@pytest.fixture
def head(db, family):
    user = User(family_id=family.id, username="marina", password_hash=hash_password("pw"),
                display_name="Марина", relation="мама", role=ROLE_HEAD, avatar_slot=0, autonomy=1)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def member(db, family):
    user = User(family_id=family.id, username="leva", display_name="Лёва", relation="сын",
                role=ROLE_MEMBER, avatar_slot=1, autonomy=0)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class FakeLLM:
    """Stands in for the model: returns a scripted sequence of responses.

    The agent loop is the thing under test, not the model, so the tests script
    exactly what the model «decides» and assert on what the runtime does with it.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def configured(self):
        return True

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            raise AssertionError("FakeLLM: запрошено больше ответов, чем задано в тесте")
        return self.responses.pop(0)

    def json_completion(self, system, user_content, **kwargs):
        if not self.responses:
            raise AssertionError("FakeLLM: запрошено больше ответов, чем задано в тесте")
        return self.responses.pop(0)
