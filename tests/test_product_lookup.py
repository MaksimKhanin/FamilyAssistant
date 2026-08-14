"""Фабричная еда считается по этикетке, а не на глаз.

«Батончик Марс» и «пицца пепперони из Додо» — это товары с опубликованным
составом, и оценка по внешнему виду расходится с ним в разы. Тесты держат три
вещи: поиск разбирается у всех провайдеров одинаково, справка не выдумывает
чисел и адресов, и найденный состав действительно доезжает до записи о еде —
а когда интернета нет, еда всё равно записывается.
"""
import pytest

from app.agent.registry import ToolContext
from app.core.config import WebSearchSettings
from app.core.websearch import SearchResult, SearchUnavailable, WebSearchClient
from app.core import websearch
from app.modules.nutrition import lookup, tools
from app.modules.nutrition.lookup import Macros, ProductFacts
from app.modules.nutrition.vision import MealEstimate


@pytest.fixture(autouse=True)
def clean_cache():
    lookup.forget_all()
    yield
    lookup.forget_all()


@pytest.fixture
def ctx(db, head):
    return ToolContext(db=db, actor=head, subject=head, channel="web", attachments={})


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


@pytest.fixture
def http(monkeypatch):
    """Подменяет поход в сеть и запоминает, о чём именно спросили."""
    sent = {}

    def fake_request(**kwargs):
        sent.update(kwargs)
        return FakeResponse(sent.get("payload", {}))

    monkeypatch.setattr(websearch.httpx, "request", fake_request)
    return sent


class FakeSearch:
    """Поисковик, который отдаёт заготовленные выдержки."""

    def __init__(self, results, configured=True):
        self.results = results
        self.configured = configured
        self.queries = []

    def search(self, query, count=None):
        self.queries.append(query)
        if not self.configured:
            raise SearchUnavailable("выключено")
        return self.results


class FakeLLMJson:
    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def json_completion(self, system, user_content, **kwargs):
        self.prompts.append(user_content)
        return self.payload


MARS = [
    SearchResult(title="Mars 51 г", url="https://www.mars.com/mars-51",
                 snippet="Батончик Mars 51 г. Пищевая ценность на 100 г: 449 ккал, "
                         "белки 4 г, жиры 17 г, углеводы 68 г."),
    SearchResult(title="Калорийность батончика", url="https://calorizator.ru/mars",
                 snippet="Mars: 449 ккал на 100 грамм."),
]

MARS_FACTS = {"title": "Батончик Mars 51 г",
              "per_100g": {"kcal": 449, "protein": 4, "fat": 17, "carbs": 68},
              "portion": "батончик 51 г", "portion_g": 51,
              "sources": ["https://www.mars.com/mars-51"], "confidence": "high"}


# --- поиск ----------------------------------------------------------------

def test_every_provider_gives_back_the_same_three_fields(http):
    """Формат ответа у всех свой, а нужны от них заголовок, адрес и выдержка."""
    cases = {
        "tavily": {"results": [{"title": "Mars", "url": "https://mars.com", "content": "449 ккал"}]},
        "brave": {"web": {"results": [{"title": "Mars", "url": "https://mars.com",
                                       "description": "449 ккал"}]}},
        "searxng": {"results": [{"title": "Mars", "url": "https://mars.com", "content": "449 ккал"}]},
    }
    for provider, payload in cases.items():
        http["payload"] = payload
        client = WebSearchClient(WebSearchSettings(provider=provider, api_key="k",
                                                  base_url="https://search.local"))

        results = client.search("батончик марс калорийность")

        assert [(r.title, r.url, r.snippet) for r in results] == [
            ("Mars", "https://mars.com", "449 ккал")], provider
        assert results[0].domain == "mars.com"


def test_a_result_without_an_address_is_not_a_result(http):
    """Выдержка без адреса — цифра без источника: проверить её нечем."""
    http["payload"] = {"results": [{"title": "Mars", "content": "449 ккал"},
                                   {"title": "Mars", "url": "https://mars.com", "content": "449"}]}
    client = WebSearchClient(WebSearchSettings(provider="tavily", api_key="k"))

    assert [r.url for r in client.search("марс")] == ["https://mars.com"]


def test_without_a_provider_the_assistant_does_not_go_outside():
    client = WebSearchClient(WebSearchSettings())

    assert not client.configured
    with pytest.raises(SearchUnavailable):
        client.search("что угодно")


def test_a_network_failure_is_not_a_crash(monkeypatch):
    import httpx

    def boom(**kwargs):
        raise httpx.ConnectError("нет сети")

    monkeypatch.setattr(websearch.httpx, "request", boom)
    client = WebSearchClient(WebSearchSettings(provider="tavily", api_key="k"))

    with pytest.raises(SearchUnavailable):
        client.search("марс")


# --- разбор справки -------------------------------------------------------

def test_the_portion_is_counted_from_the_hundred_grams():
    """Умножение на вес порции — работа кода: модель ошибается на нём чаще всего."""
    facts = lookup._coerce(MARS_FACTS, "батончик марс", MARS)

    assert facts.per_100g == Macros(449, 4, 17, 68)
    assert facts.per_portion == Macros(229, 2, 9, 35)
    assert facts.portion == "батончик 51 г"
    assert facts.confidence == "high"


def test_a_source_nobody_opened_is_dropped():
    """Модель охотно дописывает правдоподобный адрес — в справку он не попадает."""
    raw = dict(MARS_FACTS, sources=["https://www.mars.com/mars-51", "https://vydumka.example/mars"])

    facts = lookup._coerce(raw, "батончик марс", MARS)

    assert facts.sources == ["https://www.mars.com/mars-51"]
    assert facts.domains == ["mars.com"]


def test_a_lookup_without_numbers_is_the_same_as_no_lookup():
    facts = lookup._coerce({"title": "Что-то", "confidence": "low"}, "нечто", MARS)

    assert not facts.known


def test_the_facts_reach_the_estimator_as_words():
    facts = lookup._coerce(MARS_FACTS, "батончик марс", MARS)

    prompt = facts.as_prompt()

    assert "на 100 г: 449 ккал" in prompt
    assert "батончик 51 г" in prompt
    assert "mars.com" in prompt


def test_the_same_product_is_looked_up_once():
    """Семья за обедом спрашивает про одно и то же по нескольку раз."""
    search, llm = FakeSearch(MARS), FakeLLMJson(MARS_FACTS)

    lookup.lookup("батончик марс", llm=llm, search=search)
    lookup.lookup("  Батончик  Марс ", llm=llm, search=search)

    assert len(search.queries) == 1


def test_an_empty_search_result_does_not_reach_the_model():
    search, llm = FakeSearch([]), FakeLLMJson(MARS_FACTS)

    with pytest.raises(SearchUnavailable):
        lookup.lookup("несуществующий товар", llm=llm, search=search)
    assert llm.prompts == []


def test_without_search_the_lookup_is_simply_skipped():
    assert lookup.safe_lookup("батончик марс", search=FakeSearch([], configured=False)) is None


# --- запись еды -----------------------------------------------------------

@pytest.fixture
def estimator(monkeypatch):
    """Оценщик, который узнаёт товар с первого раза и считает по справке со второго."""
    calls = []

    def fake(text, context=None, facts=None, llm=None):
        calls.append({"text": text, "facts": facts})
        if facts:
            return MealEstimate(title="Батончик Mars 51 г", kcal=229, protein=2, fat=9, carbs=35,
                                portion="батончик 51 г", confidence="high")
        return MealEstimate(title="Шоколадный батончик", kcal=350, protein=4, fat=15, carbs=50,
                            portion="батончик", brand="батончик Mars 51 г")

    monkeypatch.setattr(tools, "safe_estimate_from_text", fake)
    return calls


@pytest.fixture
def found(monkeypatch):
    facts = ProductFacts(query="батончик Mars 51 г", title="Батончик Mars 51 г",
                         per_100g=Macros(449, 4, 17, 68), per_portion=Macros(229, 2, 9, 35),
                         portion="батончик 51 г", portion_g=51,
                         sources=["https://www.mars.com/mars-51"], confidence="high")
    monkeypatch.setattr(lookup, "safe_lookup", lambda name, **kwargs: facts)
    return facts


def test_a_named_product_is_counted_by_its_label(ctx, estimator, found):
    result = tools.log_meal(ctx, text="съел батончик марс")

    assert "229 ккал" in result.summary            # с этикетки, а не 350 на глаз
    assert estimator[1]["facts"] is not None       # второй проход получил справку
    assert result.data["sources"] == ["https://www.mars.com/mars-51"]


def test_the_person_is_told_where_the_numbers_came_from(ctx, db, head, estimator, found):
    """Цифра без источника ничем не отличается от выдуманной."""
    result = tools.log_meal(ctx, text="съел батончик марс")

    assert "mars.com" in result.summary
    meal = db.query(tools.service.Meal).filter(tools.service.Meal.user_id == head.id).one()
    assert "mars.com" in (meal.note or "")         # источник переживает разговор


def test_home_food_does_not_go_to_the_internet(ctx, estimator, monkeypatch):
    """У борща этикетки нет — ходить за ней некуда и незачем."""
    asked = []
    monkeypatch.setattr(lookup, "safe_lookup", lambda name, **kwargs: asked.append(name))
    monkeypatch.setattr(tools, "safe_estimate_from_text",
                        lambda text, context=None, facts=None, llm=None: MealEstimate(
                            title="Борщ", kcal=380, protein=14, fat=18, carbs=38))

    result = tools.log_meal(ctx, text="съел тарелку борща")

    assert "380 ккал" in result.summary
    assert asked == []


def test_a_silent_internet_does_not_cost_the_record(ctx, estimator, monkeypatch):
    """Поиск — приятное дополнение к оценке, а не условие её появления."""
    monkeypatch.setattr(lookup, "safe_lookup", lambda name, **kwargs: None)

    result = tools.log_meal(ctx, text="съел батончик марс")

    assert "350 ккал" in result.summary            # осталась первая оценка
    assert len(estimator) == 1                     # второго прохода не было


def test_the_photo_of_a_wrapper_is_counted_by_the_label(ctx, monkeypatch, found):
    """На фото видно товар — считаем по составу, а не по виду упаковки."""
    calls = []

    def fake_image(image_bytes, hint=None, context=None, facts=None, llm=None):
        calls.append(facts)
        if facts:
            return MealEstimate(title="Батончик Mars 51 г", kcal=229, protein=2, fat=9, carbs=35)
        return MealEstimate(title="Батончик", kcal=350, protein=4, fat=15, carbs=50,
                            brand="батончик Mars 51 г")

    monkeypatch.setattr(tools, "estimate_from_image", fake_image)
    ctx.attachments["image"] = b"jpeg-bytes"

    result = tools.log_meal(ctx, text="это на обед")

    assert "229 ккал" in result.summary
    assert calls[1] is not None


# --- инструмент справки ---------------------------------------------------

def test_the_lookup_tool_names_the_numbers_and_the_source(ctx, monkeypatch, found):
    monkeypatch.setattr(lookup, "lookup", lambda name, **kwargs: found)
    monkeypatch.setattr(lookup, "available", lambda *a, **kw: True)

    result = tools.lookup_product(ctx, name="батончик марс")

    assert result.ok
    assert "449 ккал" in result.summary
    assert "mars.com" in result.summary
    assert result.data["per_portion"]["kcal"] == 229


def test_the_lookup_tool_says_plainly_that_it_has_no_internet(ctx, monkeypatch):
    monkeypatch.setattr(lookup, "available", lambda *a, **kw: False)

    result = tools.lookup_product(ctx, name="батончик марс")

    assert not result.ok
    assert "не настроен" in result.summary
