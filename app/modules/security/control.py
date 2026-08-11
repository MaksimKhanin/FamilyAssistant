"""Пульт: команды рекордеру, который стоит дома.

Сервер не умеет разговаривать с камерами напрямую — RTSP-доступы намеренно не
покидают дом. Зато у рекордера есть маленький токенный HTTP-API, и панель просто
пересылает туда нажатие кнопки. Канал предполагается приватным (Tailscale /
WireGuard), поэтому наружу этот адрес не публикуется.
"""
from typing import Tuple

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("security.control")

#: Что рекордер умеет по каждой камере — список задан его же `ACTIONS`.
ACTIONS = {
    "alarm-on": "Тревога включена",
    "alarm-off": "Тревога выключена",
    "photo": "Снимок заказан",
    "record": "Запись заказана",
}

TIMEOUT = 10.0


def configured() -> bool:
    return bool(settings.control_base_url)


async def send(camera: str, action: str) -> Tuple[bool, str]:
    """Отправить команду. Возвращает (получилось, что сказать человеку).

    Ошибку не поднимаем: кнопка нажимается с телефона, и «рекордер сейчас не
    отвечает» — это сообщение на экране, а не страница с ошибкой.
    """
    if action not in ACTIONS:
        return False, "Неизвестная команда"
    if not configured():
        return False, "Пульт рекордера не настроен"

    url = f"{settings.control_base_url.rstrip('/')}/control/{camera}/{action}"
    headers = {"Authorization": f"Bearer {settings.control_api_token}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"Рекордер отклонил команду {action} для {camera}: {e.response.status_code}")
        return False, "Рекордер не принял команду"
    except httpx.HTTPError as e:
        logger.error(f"Рекордер недоступен ({action} для {camera}): {e}")
        return False, "Рекордер сейчас не отвечает"

    logger.info(f"Команда рекордеру: камера={camera}, действие={action}")
    return True, ACTIONS[action]
