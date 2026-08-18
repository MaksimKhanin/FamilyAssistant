"""Озвучка — чтение вслух того, что ассистент написал.

Настройка двухчастная, и части разной природы, поэтому и живут они в разных
местах:

  * **чем читать** — решает администратор на экране «Модель и знания», одно на
    семью. Это выбор того же рода, что ядро или зрение: голосом устройства
    (синтез в браузере — бесплатно, без ключей, ничего не уходит из дома) или
    моделью озвучки (живой голос, но текст ответа уезжает провайдеру). Такое не
    выбирают каждый себе — это решение про дом;
  * **читать ли вслух** — решает каждый участник у себя в профиле. Одному в
    машине удобно слушать, другому за столом это мешает, и переспрашивать друг
    друга здесь не о чем.

Адрес и ключ модели знает только окружение (`SPEECH_*`), как и у самой модели:
в панели выбирают поведение, а не пароли. Пока имя модели не названо, вариант
«Модель озвучки» на экране недоступен — обещать голос, которого нет, хуже, чем
честно оставить один работающий.

Озвучивается ровно текст ответа — тот, что человек и так видит пузырём. Ни
карточки, ни след инструментов вслух не читаются: слушают разговор, а не
интерфейс.
"""
from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.models import FamilySettings

logger = get_logger("speech")

#: Кем читать. Значения хранятся в `family_settings.speech_mode`.
MODES = [
    ("device", "Голос устройства",
     "Читает сам телефон или браузер. Бесплатно и не уходит из дома, но голос роботный"),
    ("model", "Модель озвучки",
     "Живой голос с сервера. Текст ответа уезжает провайдеру, и за него платят как за модель"),
]

#: Голоса OpenAI-совместимого `/audio/speech`. Список короткий нарочно: это
#: стандартный набор, который понимают и OpenAI, и совместимые сборки. Провайдер
#: со своими именами голосов сюда не поместится — но и выбирать их будет некому:
#: на экране нужен список, а не поле для строки, которую негде подсмотреть.
VOICES = {
    "alloy": "Ровный, нейтральный",
    "echo": "Мужской, спокойный",
    "fable": "Тёплый, с интонацией",
    "onyx": "Низкий мужской",
    "nova": "Женский, живой",
    "shimmer": "Мягкий женский",
}

MODE_KEYS = {key for key, _, _ in MODES}

DEFAULT_MODE = "device"
DEFAULT_VOICE = "alloy"

#: Скорость чтения в процентах от обычной. Ниже 70 речь звучит пьяной, выше 140
#: перестаёт разбираться на слух — крайности отрезаны, а не оставлены человеку.
RATE_MIN, RATE_MAX, RATE_STEP = 70, 140, 10
DEFAULT_RATE = 100

#: Сколько текста уезжает на озвучку за раз. Ответы ассистента короткие; всё,
#: что длиннее, — это пересказ доски или список, и слушать его целиком всё равно
#: никто не станет, а платить за каждый знак пришлось бы.
TEXT_LIMIT = 800


class SpeechUnavailable(RuntimeError):
    """Модель озвучки не ответила. Панель в этом случае читает голосом устройства."""


@dataclass
class SpeechChoice:
    """Что панель знает об озвучке для одного человека.

    `mode` — что выбрала семья, `effective_mode` — чем читать на самом деле:
    если модель озвучки в окружении не задана, вместо неё остаётся устройство.
    Разделение нужно обеим сторонам — экрану, чтобы показать выбор семьи, и
    браузеру, чтобы не ходить за звуком, которого не будет.
    """
    mode: str = DEFAULT_MODE
    voice: str = DEFAULT_VOICE
    rate: int = DEFAULT_RATE
    #: Включил ли человек озвучку себе.
    enabled: bool = False
    #: Названа ли модель озвучки в окружении.
    available: bool = False

    @property
    def effective_mode(self) -> str:
        return "model" if self.mode == "model" and self.available else "device"


def normalize_rate(value) -> int:
    try:
        rate = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RATE
    rate = max(RATE_MIN, min(RATE_MAX, rate))
    return rate - rate % RATE_STEP


def choice(settings_row: FamilySettings, user) -> SpeechChoice:
    """Озвучка глазами одного участника: выбор семьи плюс его собственный тумблер."""
    return SpeechChoice(
        mode=settings_row.speech_mode if settings_row.speech_mode in MODE_KEYS else DEFAULT_MODE,
        voice=settings_row.speech_voice if settings_row.speech_voice in VOICES else DEFAULT_VOICE,
        rate=normalize_rate(settings_row.speech_rate),
        enabled=bool(getattr(user, "speech_enabled", False)),
        available=settings.speech.configured,
    )


def synthesize(text: str, voice: str = DEFAULT_VOICE, rate: int = DEFAULT_RATE) -> bytes:
    """Озвучить текст моделью и вернуть готовый звук.

    Ходим тем же способом, что и в модель, — обычным HTTP по OpenAI-совместимому
    адресу (`app/agent/llm.py`): под ним работают и OpenAI, и локальные сборки, и
    вендорский SDK ради одного запроса тянуть незачем.
    """
    if not settings.speech.configured:
        raise SpeechUnavailable("Модель озвучки не настроена")

    body = {
        "model": settings.speech.model,
        "voice": voice if voice in VOICES else DEFAULT_VOICE,
        "input": (text or "").strip()[:TEXT_LIMIT],
        "response_format": settings.speech.audio_format,
        # Провайдер ждёт множитель, а панель хранит проценты: человеку понятнее
        # «120 %», чем «1.2».
        "speed": round(normalize_rate(rate) / 100, 2),
    }
    if not body["input"]:
        raise SpeechUnavailable("Нечего озвучивать")

    headers = {"Content-Type": "application/json"}
    if settings.speech.api_key:
        headers["Authorization"] = f"Bearer {settings.speech.api_key}"

    url = settings.speech.base_url.rstrip("/") + "/audio/speech"
    try:
        response = httpx.post(url, json=body, headers=headers,
                              timeout=settings.speech.request_timeout)
    except httpx.HTTPError as e:
        logger.warning("озвучка не удалась: %s", e)
        raise SpeechUnavailable(str(e))

    if response.status_code >= 400:
        logger.warning("озвучка не удалась: %s %s", response.status_code, response.text[:200])
        raise SpeechUnavailable(f"Модель озвучки ответила {response.status_code}")
    return response.content


def media_type() -> str:
    return {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac",
            "flac": "audio/flac", "wav": "audio/wav", "pcm": "audio/wave"}.get(
                settings.speech.audio_format, "audio/mpeg")
