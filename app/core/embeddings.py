"""Эмбеддинги — числовое представление текста для поиска по смыслу.

Клиент того же рода, что `speech.synthesize`: голый HTTP к OpenAI-совместимому
`/embeddings`, адрес и ключ знает только окружение (`EMBED_*`, по умолчанию —
те же, что у модели). Пока имя модели эмбеддингов не названо, клиент честно
«не настроен», и всё, что на нём держится (семантический recall), живёт без
него — подстрочным поиском, как всегда.

Векторы хранятся упакованными float32-блобами (`pack`/`unpack`) в обычной
таблице — одинаково в SQLite и Postgres, без нативных расширений. Косинус
считается в Python: корпус семьи — сотни записей, у досок капы
(NOTES_MAX=50 и т.п.), и полный перебор — миллисекунды (ADR-0017).
"""
import math
import struct
from typing import List

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("embeddings")


class EmbeddingsUnavailable(RuntimeError):
    """Модель эмбеддингов не настроена или не ответила. Вызывающий обходится
    без поиска по смыслу — это деградация, а не авария."""


class EmbeddingsClient:
    def __init__(self, cfg=None):
        self.cfg = cfg or settings.embeddings

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Векторы для списка текстов, в том же порядке."""
        if not self.configured:
            raise EmbeddingsUnavailable(
                "Эмбеддинги не настроены: задайте EMBED_MODEL (и при нужде EMBED_BASE_URL)")
        if not texts:
            return []
        url = self.cfg.base_url.rstrip("/") + "/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        try:
            response = httpx.post(url, headers=headers, timeout=self.cfg.request_timeout,
                                  json={"model": self.cfg.model, "input": texts})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"Эмбеддинги не посчитались: {e}")
            raise EmbeddingsUnavailable("Модель эмбеддингов недоступна") from e

        rows = sorted(payload.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [row.get("embedding") for row in rows]
        if len(vectors) != len(texts) or any(not isinstance(v, list) for v in vectors):
            raise EmbeddingsUnavailable("Модель эмбеддингов ответила не тем форматом")
        return vectors


def pack(vector: List[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack(blob: bytes) -> List[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


client = EmbeddingsClient()
