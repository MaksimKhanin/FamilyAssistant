"""Поиск в интернете — единственная дверь наружу к чужому поисковому API.

Как и клиент модели, этот модуль ничего не знает о предметной области: он умеет
задать вопрос поисковику и вернуть заголовок, адрес и выдержку. Что с этим делать —
дело модуля (сегодня это питание: `app/modules/nutrition/lookup.py`).

Провайдер выбирается одной переменной окружения, потому что домашние установки
разные: у кого-то ключ Tavily, у кого-то Brave, у кого-то свой SearXNG в домашней
сети и никаких ключей вовсе. Формат ответа у всех троих свой, а нужен от них один
и тот же список выдержек — поэтому у каждого свой разбор и общий `SearchResult`.

По умолчанию поиск выключен: без `WEB_SEARCH_PROVIDER` ассистент наружу не ходит.
"""
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from app.agent import tracing
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("websearch")

#: Сколько знаков выдержки доезжает до модели. Поисковики отдают до нескольких
#: килобайт на результат, а состав продукта живёт в первых строках.
SNIPPET_LIMIT = 600


class SearchUnavailable(RuntimeError):
    """Поиск не настроен или не отвечает.

    Как и `LLMUnavailable`, это не авария: вызывающий обходится без интернета —
    ассистент считает по своей оценке и говорит об этом.
    """


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    @property
    def domain(self) -> str:
        return (urlparse(self.url).netloc or "").removeprefix("www.")


def _clean(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _results(raw: Any, title_keys, url_keys, snippet_keys) -> List[SearchResult]:
    found = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        url = _clean(*(item.get(k) for k in url_keys))
        if not url:
            continue
        found.append(SearchResult(
            title=_clean(*(item.get(k) for k in title_keys))[:200] or url,
            url=url,
            snippet=_clean(*(item.get(k) for k in snippet_keys))[:SNIPPET_LIMIT],
        ))
    return found


@dataclass(frozen=True)
class Provider:
    """Как спросить одного поисковика и как разобрать его ответ."""
    default_base_url: str
    #: (настройки, адрес, запрос, сколько результатов) → аргументы httpx.request
    request: Callable[..., Dict[str, Any]]
    parse: Callable[[dict], List[SearchResult]]


def _tavily_request(cfg, url: str, query: str, count: int) -> Dict[str, Any]:
    return {
        "method": "POST",
        "url": f"{url}/search",
        "headers": {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"},
        "json": {"query": query, "max_results": count, "search_depth": "basic"},
    }


def _brave_request(cfg, url: str, query: str, count: int) -> Dict[str, Any]:
    return {
        "method": "GET",
        "url": f"{url}/res/v1/web/search",
        "headers": {"X-Subscription-Token": cfg.api_key, "Accept": "application/json"},
        "params": {"q": query, "count": count, "search_lang": cfg.lang},
    }


def _searxng_request(cfg, url: str, query: str, count: int) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if cfg.api_key:                       # у домашнего SearXNG ключа обычно нет
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return {
        "method": "GET",
        "url": f"{url}/search",
        "headers": headers,
        "params": {"q": query, "format": "json", "language": cfg.lang},
    }


PROVIDERS: Dict[str, Provider] = {
    "tavily": Provider(
        default_base_url="https://api.tavily.com",
        request=_tavily_request,
        parse=lambda data: _results(data.get("results"), ("title",), ("url",), ("content", "raw_content")),
    ),
    "brave": Provider(
        default_base_url="https://api.search.brave.com",
        request=_brave_request,
        parse=lambda data: _results((data.get("web") or {}).get("results"),
                                    ("title",), ("url",), ("description", "snippet")),
    ),
    "searxng": Provider(
        default_base_url="",
        request=_searxng_request,
        parse=lambda data: _results(data.get("results"), ("title",), ("url",), ("content",)),
    ),
}


class WebSearchClient:
    def __init__(self, cfg=None):
        self.cfg = cfg or settings.web_search

    @property
    def provider(self) -> Optional[Provider]:
        return PROVIDERS.get(self.cfg.provider)

    @property
    def configured(self) -> bool:
        return bool(self.provider) and self.cfg.configured

    def search(self, query: str, count: int = None) -> List[SearchResult]:
        """Спросить поисковика. Ошибка сети или чужой формат — `SearchUnavailable`."""
        provider = self.provider
        if provider is None or not self.cfg.configured:
            raise SearchUnavailable(
                "Поиск в интернете не настроен: задайте WEB_SEARCH_PROVIDER и WEB_SEARCH_API_KEY")

        count = count or self.cfg.max_results
        base_url = (self.cfg.base_url or provider.default_base_url).rstrip("/")
        if not base_url:
            raise SearchUnavailable(f"Для провайдера {self.cfg.provider} нужен WEB_SEARCH_BASE_URL")

        request = provider.request(self.cfg, base_url, query, count)
        started = time.monotonic()
        try:
            response = httpx.request(timeout=self.cfg.timeout, **request)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"Поиск «{query}» не удался: {e}")
            self._trace(query, {"ошибка": str(e)}, started, status="failed")
            raise SearchUnavailable("Поисковик недоступен") from e

        results = provider.parse(data if isinstance(data, dict) else {})[:count]
        self._trace(query, [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results],
                    started)
        return results

    def _trace(self, query: str, answer: Any, started: float, status: str = "ok"):
        """Отдать поиск писарю трейсов: в разборе «почему такие цифры» это первое,
        что хочется увидеть, — что именно ассистент спросил у интернета."""
        recorder = tracing.current()
        if recorder is None:
            return
        recorder.tool(f"web_search:{self.cfg.provider}", {"query": query}, answer,
                      status=status, duration_ms=int((time.monotonic() - started) * 1000))


client = WebSearchClient()
