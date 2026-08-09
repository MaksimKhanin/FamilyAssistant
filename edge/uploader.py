"""Uploads detections to the server's ingest endpoint.

Failures are survivable by design: the house keeps watching even when the server
or the link is down, and a frame that cannot be delivered after a few attempts is
dropped rather than queued forever on a home machine's disk.
"""
from datetime import datetime
from typing import Optional

import cv2
import httpx

from app.core.logging import get_logger

logger = get_logger("edge.uploader")

INGEST_PATH = "/api/security/events"
MAX_ATTEMPTS = 3


class Uploader:
    def __init__(self, server_url: str, api_key: str, family_id: int = 0, timeout: float = 30.0):
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.family_id = family_id
        self.timeout = timeout

    def send(self, camera: str, class_name: str, confidence: float, area: int,
             frame, captured_at: Optional[datetime] = None) -> bool:
        captured_at = captured_at or datetime.utcnow()

        ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            logger.error("Не удалось закодировать кадр в JPEG")
            return False

        data = {
            "camera": camera,
            "detected_class": class_name,
            "confidence": f"{confidence:.4f}",
            "area": str(area),
            "captured_at": captured_at.isoformat(),
        }
        if self.family_id:
            data["family_id"] = str(self.family_id)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = httpx.post(
                    f"{self.server_url}{INGEST_PATH}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data=data,
                    files={"snapshot": (f"{camera}.jpg", buffer.tobytes(), "image/jpeg")},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                verdict = response.json().get("verdict", "?")
                logger.info(f"Событие с камеры {camera} отправлено, вердикт сервера: {verdict}")
                return True
            except httpx.HTTPError as e:
                logger.warning(f"Не отправилось ({attempt}/{MAX_ATTEMPTS}) с камеры {camera}: {e}")

        logger.error(f"Событие с камеры {camera} отброшено — сервер недоступен")
        return False
