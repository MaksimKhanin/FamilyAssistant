"""Environment-based settings for the whole assistant (server side).

Everything the core, the agent layer and the modules need is read once at import
time. Secrets never get defaults — a missing one fails loudly at startup rather
than silently degrading into an insecure mode.
"""
import os
from dataclasses import dataclass, field
from typing import List


def _require(name: str, default: str = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Обязательная переменная окружения не задана: {name}")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass
class LLMSettings:
    """OpenAI-compatible endpoint — works with OpenAI, Ollama, vLLM, LM Studio, ..."""
    base_url: str
    api_key: str
    model: str
    vision_model: str
    temperature: float
    max_tokens: int
    request_timeout: float

    @property
    def configured(self) -> bool:
        return bool(self.model) and bool(self.base_url)


@dataclass
class Settings:
    database_url: str
    media_root: str
    session_secret: str
    cookie_secure: bool
    ingest_api_key: str
    redis_url: str
    telegram_token: str
    public_base_url: str
    timezone: str
    media_retention_days: int
    llm: LLMSettings = field(default_factory=lambda: LLMSettings("", "", "", "", 0.3, 1024, 60.0))


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./family_assistant.db"),
        media_root=os.environ.get("MEDIA_ROOT", "./media"),
        session_secret=_require("SESSION_SECRET", "dev-insecure-secret" if _flag("DEV_MODE", False) else None),
        cookie_secure=_flag("COOKIE_SECURE", True),
        ingest_api_key=os.environ.get("INGEST_API_KEY", ""),
        redis_url=os.environ.get("REDIS_URL", ""),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000"),
        timezone=os.environ.get("TIMEZONE", "Europe/Moscow"),
        media_retention_days=_int("MEDIA_RETENTION_DAYS", 14),
        llm=LLMSettings(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            vision_model=os.environ.get("LLM_VISION_MODEL", "") or os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.3")),
            max_tokens=_int("LLM_MAX_TOKENS", 1024),
            request_timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
        ),
    )


settings = load_settings()

#: Modules the app assembles itself from. Adding a module means adding a package
#: under app/modules/<name>/ that exports `module` (see app/core/module.py) and
#: listing it here — the core knows nothing about any module specifically.
ENABLED_MODULES: List[str] = [
    name.strip()
    for name in os.environ.get("ENABLED_MODULES", "nutrition,security,memory").split(",")
    if name.strip()
]
