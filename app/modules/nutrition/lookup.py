"""Справка о товаре: КБЖУ фабричной еды, найденное в интернете.

Оценка на глаз хороша для тарелки борща и плоха для «батончика Марс»: у товара с
этикеткой состав опубликован, и списать его точнее, чем прикинуть. Здесь и живёт
этот обход: поисковик даёт выдержки, модель вынимает из них числа, и дальше
оценщик считает съеденное уже по составу, а не по памяти.

Модель тут не вспоминает, а читает (см. `PRODUCT_FACTS_SYSTEM`). Всё, чего в
выдачах нет, остаётся нулём: справки без чисел не бывает — бывает её отсутствие,
и тогда блюдо считается обычной оценкой.

Место в архитектуре то же, что у `vision.py`: агент решает, *что* пора оценить,
а как именно — знает модуль питания.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.agent.llm import LLMClient, LLMUnavailable, client as default_llm
from app.agent.prompts import PRODUCT_FACTS_SYSTEM
from app.core.logging import get_logger
from app.core.websearch import SearchResult, SearchUnavailable, WebSearchClient, client as default_search

logger = get_logger("nutrition.lookup")

#: Сколько живёт найденная справка. Состав батончика за сутки не меняется, а
#: семья, севшая обедать, спрашивает про одно и то же по нескольку раз.
CACHE_TTL = 24 * 60 * 60
CACHE_LIMIT = 128

_cache: Dict[str, Tuple[float, "ProductFacts"]] = {}


@dataclass
class Macros:
    kcal: int = 0
    protein: int = 0
    fat: int = 0
    carbs: int = 0

    @property
    def known(self) -> bool:
        return self.kcal > 0

    def scaled(self, share: float) -> "Macros":
        return Macros(*(int(round(value * share))
                        for value in (self.kcal, self.protein, self.fat, self.carbs)))

    def __str__(self) -> str:
        return f"{self.kcal} ккал, Б {self.protein} / Ж {self.fat} / У {self.carbs}"


@dataclass
class ProductFacts:
    query: str
    title: str
    per_100g: Optional[Macros] = None
    per_portion: Optional[Macros] = None
    portion: Optional[str] = None
    portion_g: Optional[float] = None
    sources: List[str] = field(default_factory=list)
    confidence: str = "medium"
    note: Optional[str] = None

    @property
    def known(self) -> bool:
        """Есть ли хоть какие-то числа. Пустая справка не лучше её отсутствия."""
        return bool((self.per_100g and self.per_100g.known) or (self.per_portion and self.per_portion.known))

    @property
    def domains(self) -> List[str]:
        seen = []
        for url in self.sources:
            domain = url.split("//")[-1].split("/")[0].removeprefix("www.")
            if domain and domain not in seen:
                seen.append(domain)
        return seen

    def as_prompt(self) -> str:
        """Блок для оценщика: то же самое, но словами, которые понимает промпт."""
        lines = [f"Справка о товаре из интернета — «{self.title}»:"]
        if self.per_100g and self.per_100g.known:
            lines.append(f"- на 100 г: {self.per_100g}")
        if self.per_portion and self.per_portion.known:
            lines.append(f"- на порцию ({self.portion or 'штатная порция'}): {self.per_portion}")
        elif self.portion:
            lines.append(f"- штатная порция: {self.portion}")
        if self.note:
            lines.append(f"- {self.note}")
        if self.domains:
            lines.append("- источники: " + ", ".join(self.domains))
        lines.append("Считай по этим числам, умножив их на съеденное количество.")
        return "\n".join(lines)

    def summary(self) -> str:
        """Фраза человеку — с источником, потому что цифра без источника ничем не
        отличается от выдуманной."""
        parts = [f"{self.title}:"]
        if self.per_100g and self.per_100g.known:
            parts.append(f"на 100 г — {self.per_100g}.")
        if self.per_portion and self.per_portion.known:
            parts.append(f"Порция ({self.portion or 'штатная'}) — {self.per_portion}.")
        if self.note:
            parts.append(self.note.rstrip(".") + ".")
        if self.domains:
            parts.append("Нашёл на: " + ", ".join(self.domains) + ".")
        if self.confidence == "low":
            parts.append("Данные разошлись, цифры приблизительные.")
        return " ".join(parts)


def available(search: WebSearchClient = None) -> bool:
    """Настроен ли выход в интернет. Не настроен — молча считаем на глаз."""
    return (search or default_search).configured


def _macros(raw) -> Optional[Macros]:
    if not isinstance(raw, dict):
        return None

    def number(key: str) -> int:
        try:
            return max(0, int(round(float(raw.get(key) or 0))))
        except (TypeError, ValueError):
            return 0

    macros = Macros(number("kcal"), number("protein"), number("fat"), number("carbs"))
    return macros if macros.known else None


def _coerce(raw: dict, query: str, results: List[SearchResult]) -> ProductFacts:
    per_100g = _macros(raw.get("per_100g"))
    per_portion = _macros(raw.get("per_portion"))

    try:
        portion_g = float(raw.get("portion_g") or 0) or None
    except (TypeError, ValueError):
        portion_g = None

    # Одну величину из другой считает код, а не модель: умножение на вес порции —
    # ровно та арифметика, на которой модель ошибается чаще всего.
    if per_100g and not per_portion and portion_g:
        per_portion = per_100g.scaled(portion_g / 100.0)
    elif per_portion and not per_100g and portion_g:
        per_100g = per_portion.scaled(100.0 / portion_g)

    # Ссылками считаем только то, что и правда было в выдаче: модель охотно
    # дописывает правдоподобный адрес, которого никто не открывал.
    seen = {result.url for result in results}
    sources = [str(url) for url in (raw.get("sources") or [])[:4] if str(url) in seen]

    confidence = str(raw.get("confidence", "medium")).lower()
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"

    return ProductFacts(
        query=query,
        title=(str(raw.get("title") or "").strip() or query)[:128],
        per_100g=per_100g,
        per_portion=per_portion,
        portion=(str(raw["portion"])[:128] if raw.get("portion") else None),
        portion_g=portion_g,
        sources=sources or [result.url for result in results[:2]],
        confidence=confidence,
        note=(str(raw["note"])[:255] if raw.get("note") else None),
    )


def _snippets(results: List[SearchResult]) -> str:
    return "\n\n".join(
        f"[{number}] {result.title} ({result.domain})\n{result.url}\n{result.snippet}"
        for number, result in enumerate(results, start=1)
    )


def lookup(name: str, llm: LLMClient = None, search: WebSearchClient = None) -> ProductFacts:
    """Найти состав товара в интернете. Не нашлось — `SearchUnavailable`."""
    query = (name or "").strip()
    if not query:
        raise SearchUnavailable("Нечего искать: не назван товар")

    cached = _cached(query)
    if cached is not None:
        return cached

    search = search or default_search
    results = search.search(f"{query} калорийность БЖУ состав на 100 грамм")
    if not results:
        raise SearchUnavailable(f"Поиск ничего не дал по запросу «{query}»")

    raw = (llm or default_llm).json_completion(
        PRODUCT_FACTS_SYSTEM,
        f"Товар: {query}\n\nВыдержки из поиска:\n\n{_snippets(results)}",
        max_tokens=700,
    )
    facts = _coerce(raw, query, results)
    _remember(query, facts)
    return facts


def safe_lookup(name: str, llm: LLMClient = None,
                search: WebSearchClient = None) -> Optional[ProductFacts]:
    """То же, но никогда не бросает и не мешает записать еду.

    Интернет — приятное дополнение к оценке, а не условие её появления: поисковик
    молчит или модель не отвечает — блюдо всё равно посчитается на глаз.
    """
    if not available(search):
        return None
    try:
        facts = lookup(name, llm=llm, search=search)
    except (SearchUnavailable, LLMUnavailable) as e:
        logger.info(f"Справку по «{name}» получить не вышло: {e}")
        return None
    return facts if facts.known else None


# --- кэш ------------------------------------------------------------------

def _key(query: str) -> str:
    return " ".join(query.lower().split())


def _cached(query: str) -> Optional[ProductFacts]:
    entry = _cache.get(_key(query))
    if entry is None:
        return None
    stored_at, facts = entry
    if time.monotonic() - stored_at > CACHE_TTL:
        _cache.pop(_key(query), None)
        return None
    return facts


def _remember(query: str, facts: ProductFacts):
    if len(_cache) >= CACHE_LIMIT:
        oldest = min(_cache, key=lambda key: _cache[key][0])
        _cache.pop(oldest, None)
    _cache[_key(query)] = (time.monotonic(), facts)


def forget_all():
    """Сбросить кэш — нужно тестам и ручной проверке из консоли."""
    _cache.clear()
