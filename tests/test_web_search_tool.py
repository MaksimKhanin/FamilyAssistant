"""Инструмент search_web и его ворота доступности (тикет #80)."""
import pytest

from app.agent import policy
from app.core import websearch
from app.core.websearch import SearchResult, SearchUnavailable


class FakeSearch:
    """Скриптованный поисковик — по образцу FakeLLM."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.queries = []

    @property
    def configured(self):
        return True

    def search(self, query, count=None):
        self.queries.append(query)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_the_tool_is_hidden_until_search_is_configured(db, member):
    names = {s.name for s in policy.available_tools(db, member)}
    assert "search_web" not in names, "без WEB_SEARCH_PROVIDER инструмента нет"


def test_the_tool_appears_when_search_is_configured(db, member, monkeypatch):
    monkeypatch.setattr(websearch.WebSearchClient, "configured",
                        property(lambda self: True))
    names = {s.name for s in policy.available_tools(db, member)}
    assert "search_web" in names


def test_results_ride_to_the_model_with_domains(db, member, monkeypatch):
    from app.modules.web.tools import search_web

    fake = FakeSearch([[
        SearchResult(title="Погода в Москве", url="https://pogoda.example.ru/msk",
                     snippet="Сегодня +21, без осадков."),
    ]])
    monkeypatch.setattr(websearch, "client", fake)
    from app.agent.registry import ToolContext

    result = search_web(ToolContext(db=db, actor=member, subject=member), query="погода")

    assert result.ok
    assert "pogoda.example.ru" in result.summary
    assert "+21" in result.summary
    assert result.data["sources"] == ["pogoda.example.ru"]


def test_unavailable_search_stays_honest(db, member, monkeypatch):
    from app.agent.registry import ToolContext
    from app.modules.web.tools import search_web

    monkeypatch.setattr(websearch, "client", FakeSearch([SearchUnavailable("нет сети")]))

    result = search_web(ToolContext(db=db, actor=member, subject=member), query="курс евро")

    assert not result.ok
    assert "не отвечает" in result.summary


def test_web_rules_ride_only_with_the_module(db, member, monkeypatch):
    from app.agent.prompts import system_prompt

    with_web = system_prompt(member, ["memory", "web"])
    without = system_prompt(member, ["memory"])

    assert "Про интернет" in with_web
    assert "Про интернет" not in without
