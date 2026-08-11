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


def _json_env(name: str) -> dict:
    """Объект JSON из переменной окружения. Мусор — не повод не стартовать."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return {}
    import json
    try:
        value = json.loads(raw)
    except ValueError:
        print(f"[config] {name} не разобрался как JSON — игнорирую")
        return {}
    return value if isinstance(value, dict) else {}


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
    #: Режим размышления модели: off / low / medium / high, либо пусто — не трогать.
    #: По умолчанию off: ассистент занят маршрутизацией в инструменты и короткими
    #: ответами, а «думающая» модель на этом добавляет секунды на ровном месте.
    reasoning: str = "off"
    #: Режим размышления отдельно для оценочных вызовов: КБЖУ по фото и по описанию,
    #: разбор события с камеры. Ручка отдельная, потому что задача другая — не выбор
    #: инструмента, а прикидка чисел.
    #:
    #: По умолчанию всё же off. Замер на Qwen3 через OpenRouter: с `low` те же самые
    #: цифры, но 2600 токенов бюджета вместо 600 и вчетверо дольше, а при 1800 модель
    #: не успевала даже дописать JSON — весь бюджет уходил в мысли. Работу делает
    #: не размышление, а подробный промпт (см. MEAL_TEXT_SYSTEM). Если ваша модель
    #: думает дёшево — поставьте low, механизм на месте.
    reasoning_estimate: str = "off"
    #: Произвольные поля запроса для нестандартных провайдеров, JSON из LLM_EXTRA_BODY.
    #: Перекрывают всё, что подставил пресет.
    extra_body: dict = field(default_factory=dict)
    #: Офлайн-режим для пробного запуска: вместо модели — разбор ключевых слов.
    #: Включается сам, когда модель не настроена (см. `load_settings`).
    stub: bool = False

    @property
    def configured(self) -> bool:
        return self.stub or (bool(self.model) and bool(self.base_url))


@dataclass
class AdminSettings:
    """Первый вход в систему — глава семьи, заводится из окружения при первом старте.

    Дальше учётные записи нарезаются в панели: пароль отсюда применяется только к
    пустой базе и больше не трогается, чтобы смена пароля в интерфейсе не
    откатывалась при каждом рестарте.
    """
    username: str
    password: str
    display_name: str
    relation: str
    family_name: str
    #: Аварийный сброс пароля главы семьи на значение из окружения. Держать
    #: включённым не надо: снимайте сразу после того, как вошли.
    reset_password: bool

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)


@dataclass
class PushSettings:
    """Web Push (VAPID). Ключи генерируются один раз: python -m scripts.vapid_keys"""
    public_key: str
    private_key: str
    subject: str

    @property
    def configured(self) -> bool:
        return bool(self.public_key and self.private_key)


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
    #: Локальный пульт рекордера, который стоит дома. Адрес приватный (Tailscale /
    #: WireGuard): наружу он не смотрит, поэтому и токен у него свой, отдельный от
    #: ingest-ключа — доступ «читать архив» и «включить тревогу» это разные права.
    control_base_url: str = ""
    control_api_token: str = ""
    push: PushSettings = field(default_factory=lambda: PushSettings("", "", "mailto:admin@example.com"))
    admin: AdminSettings = field(
        default_factory=lambda: AdminSettings("", "", "", "", "Семья", False))
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
        control_base_url=os.environ.get("CONTROL_BASE_URL", "").strip(),
        control_api_token=os.environ.get("CONTROL_API_TOKEN", "").strip(),
        admin=AdminSettings(
            username=os.environ.get("ADMIN_USERNAME", "admin").strip(),
            password=os.environ.get("ADMIN_PASSWORD", ""),
            display_name=os.environ.get("ADMIN_NAME", "").strip(),
            relation=os.environ.get("ADMIN_RELATION", "").strip(),
            family_name=os.environ.get("FAMILY_NAME", "Семья").strip() or "Семья",
            reset_password=_flag("ADMIN_PASSWORD_RESET", False),
        ),
        push=PushSettings(
            public_key=os.environ.get("VAPID_PUBLIC_KEY", ""),
            private_key=os.environ.get("VAPID_PRIVATE_KEY", ""),
            subject=os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com"),
        ),
        llm=LLMSettings(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            vision_model=os.environ.get("LLM_VISION_MODEL", "") or os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.3")),
            max_tokens=_int("LLM_MAX_TOKENS", 1024),
            request_timeout=float(os.environ.get("LLM_TIMEOUT", "60")),
            reasoning=os.environ.get("LLM_REASONING", "off").strip().lower(),
            reasoning_estimate=os.environ.get("LLM_REASONING_ESTIMATE", "off").strip().lower(),
            extra_body=_json_env("LLM_EXTRA_BODY"),
            stub=_flag("LLM_STUB", False),
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
