"""Режиссёр модели: что «решит» модель на этом ходу — говорит сценарий.

Живая модель для проверки поведения не годится дважды. Во-первых, она разная от
запуска к запуску: тот же сценарий сегодня возьмёт `log_meal`, а завтра ответит
словами, и упавший шаг ничего не доказывает. Во-вторых, интересное в ассистенте —
не то, что модель обычно делает, а то, что бывает редко: отчиталась о работе, не
вызвав инструмента; попросила инструмент, которого нет; прислала мусор в
аргументах; не ответила вовсе. Дождаться такого от живой модели нельзя, а
подстроить — можно.

Поэтому сценарий кладёт сюда очередь ответов, а режиссёр подменяет обе двери, в
которые ходит ассистент, — `chat` (разговор с инструментами) и `json_completion`
(оценка блюда, разбор события). Подмена живёт на классе клиента, а не на одном
экземпляре: клиент лежит в модуле готовым объектом, и модули берут его себе кто
когда — перехватывать надо метод, а не ссылку.

Кончилась очередь — режиссёр отходит в сторону, и ход доигрывает то, что стояло
бы и без него: живая модель или офлайн-разбор ключевых слов. Так сценарий может
подстроить один трудный ход и не расписывать остальные; в журнале обращений
видно, какие ходы были поставлены (`scripted: true`), а какие сыграны сами.

Каждое обращение к модели пишется в кольцо целиком — с системным промптом и
перечнем инструментов. Это то же, что показывает экран трейсов, но доступное и
тогда, когда трейсы выключены или модель отвечает офлайн-разбором.
"""
import json
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.agent.llm import LLMClient, LLMResponse, LLMUnavailable, ToolCall
from app.core.websearch import SearchResult, SearchUnavailable, WebSearchClient

#: Сколько обращений к модели помнит кольцо.
KEEP_CALLS = 200

#: Сколько знаков одного сообщения переживает запись. Картинка в base64 — это
#: мегабайты, а сценарию от неё нужен факт, что она уехала.
VALUE_LIMIT = 2000

_chat_script: List[dict] = []
_json_script: List[dict] = []
_search_script: List[dict] = []
_calls: deque = deque(maxlen=KEEP_CALLS)
_call_no = 0

_original_chat = None
_original_json = None
_original_search = None
_original_configured = None


# --- сценарий -------------------------------------------------------------

def set_script(chat: List[dict] = None, json_replies: List[dict] = None,
               search: List[dict] = None):
    """Положить очередь ответов. Пустые списки очищают очередь."""
    global _chat_script, _json_script, _search_script
    _chat_script = [dict(entry) for entry in (chat or [])]
    _json_script = [dict(entry) for entry in (json_replies or [])]
    _search_script = [dict(entry) for entry in (search or [])]


def script() -> Dict[str, List[dict]]:
    return {"chat": list(_chat_script), "json": list(_json_script),
            "search": list(_search_script)}


def calls(since: int = 0, limit: int = 50) -> List[dict]:
    return [c for c in _calls if c["no"] > since][-limit:]


def count() -> int:
    """Номер последнего обращения к модели — курсор для «что было за этот ход»."""
    return _call_no


def forget_calls():
    global _call_no
    _calls.clear()
    _call_no = 0


def _take(queue: List[dict], haystack: str) -> Optional[dict]:
    """Первый подходящий ответ из очереди — и вон из неё.

    `when` делает сценарий независимым от порядка ходов: реплика «съел суп»
    находит свой ответ, даже если человек до неё поздоровался. Без `when`
    ответ берётся в порядке очереди — так пишутся короткие сценарии.
    `keep` оставляет ответ в очереди: им отвечают на любой ход подряд.
    """
    for index, entry in enumerate(queue):
        needle = (entry.get("when") or "").lower()
        if needle and needle not in haystack.lower():
            continue
        if not entry.get("keep"):
            queue.pop(index)
        return entry
    return None


def _flatten(content: Any) -> str:
    """Текст сообщения, каким бы сложным ни было его содержимое."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content or "")


def _last_user_text(messages: List[dict]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return _flatten(message.get("content"))
    return ""


def _response(entry: dict) -> LLMResponse:
    """Строчка сценария → ответ модели, как его увидит агентское ядро."""
    calls_out = []
    for number, call in enumerate(entry.get("tool_calls") or [], start=1):
        calls_out.append(ToolCall(
            id=call.get("id") or f"scripted_{number}",
            name=call.get("name") or "",
            arguments=call.get("arguments") or {},
        ))
    # Сокращённая запись на один вызов: сценарии из одного инструмента — это
    # большинство сценариев, и городить ради них список из одного словаря лишнее.
    if entry.get("tool"):
        calls_out.append(ToolCall(id=entry.get("id") or "scripted_1",
                                  name=entry["tool"],
                                  arguments=entry.get("arguments") or {}))
    return LLMResponse(
        content=entry.get("content") or "",
        tool_calls=calls_out,
        finish_reason=entry.get("finish_reason") or "stop",
        usage=entry.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    )


def _trim(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= VALUE_LIMIT else value[:VALUE_LIMIT] + "…"
    if isinstance(value, list):
        return [_trim(v) for v in value]
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    return value


def _remember(kind: str, messages: List[dict], tools, scripted: bool, answer: Any) -> dict:
    global _call_no
    _call_no += 1
    record = {
        "no": _call_no,
        "at": datetime.utcnow().isoformat(),
        "kind": kind,
        "scripted": scripted,
        "messages": [{"role": m.get("role"), "content": _trim(_flatten(m.get("content"))),
                      "tool_calls": m.get("tool_calls")} for m in messages or []],
        "tools": sorted((t.get("function") or {}).get("name", "?") for t in tools or []),
        "answer": _trim(answer),
    }
    _calls.append(record)
    return record


def _answer_view(response: LLMResponse) -> dict:
    return {
        "content": response.content,
        "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in response.tool_calls],
        "finish_reason": response.finish_reason,
    }


# --- подмена --------------------------------------------------------------

def _chat(self: LLMClient, messages, tools=None, **kwargs) -> LLMResponse:
    entry = _take(_chat_script, _last_user_text(messages))
    if entry is None:
        response = _original_chat(self, messages, tools=tools, **kwargs)
        _remember("chat", messages, tools, False, _answer_view(response))
        return response
    if entry.get("error"):
        _remember("chat", messages, tools, True, {"error": entry["error"]})
        raise LLMUnavailable(str(entry["error"]))
    response = _response(entry)
    _remember("chat", messages, tools, True, _answer_view(response))
    return response


def _json_completion(self: LLMClient, system, user_content, **kwargs) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
    entry = _take(_json_script, _flatten(user_content))
    if entry is None:
        answer = _original_json(self, system, user_content, **kwargs)
        _remember("json", messages, None, False, answer)
        return answer
    if entry.get("error"):
        _remember("json", messages, None, True, {"error": entry["error"]})
        raise LLMUnavailable(str(entry["error"]))
    answer = entry.get("json")
    if isinstance(answer, str):
        answer = json.loads(answer)
    _remember("json", messages, None, True, answer)
    return answer or {}


def _search(self: WebSearchClient, query: str, count: int = None):
    """Подмена поиска — тот же принцип, что у `_chat`: очередь кончилась —
    отвечает настоящий поисковик (или его честное «не настроен»)."""
    entry = _take(_search_script, query)
    if entry is None:
        return _original_search(self, query, count=count)
    record = [{"role": "user", "content": query}]
    if entry.get("error"):
        _remember("search", record, None, True, {"error": entry["error"]})
        raise SearchUnavailable(str(entry["error"]))
    results = [SearchResult(title=str(item.get("title") or ""),
                            url=str(item.get("url") or ""),
                            snippet=str(item.get("snippet") or ""))
               for item in entry.get("results") or []]
    _remember("search", record, None, True,
              [{"title": r.title, "url": r.url, "snippet": r.snippet} for r in results])
    return results


def _configured(self: WebSearchClient) -> bool:
    # Со скриптованной выдачей поиск «настроен», даже если окружение стенда
    # не знает ни одного провайдера: иначе search_web не попал бы в схему
    # модели и сценарию нечего было бы проверять.
    if _search_script:
        return True
    return _original_configured.fget(self)


def install():
    """Встать между ассистентом и моделью. Идемпотентно."""
    global _original_chat, _original_json, _original_search, _original_configured
    if _original_chat is not None:
        return
    _original_chat, _original_json = LLMClient.chat, LLMClient.json_completion
    LLMClient.chat = _chat
    LLMClient.json_completion = _json_completion
    _original_search = WebSearchClient.search
    _original_configured = WebSearchClient.configured
    WebSearchClient.search = _search
    WebSearchClient.configured = property(_configured)


def uninstall():
    """Вернуть всё как было — для тестов самого стенда."""
    global _original_chat, _original_json, _original_search, _original_configured
    if _original_chat is None:
        return
    LLMClient.chat, LLMClient.json_completion = _original_chat, _original_json
    WebSearchClient.search = _original_search
    WebSearchClient.configured = _original_configured
    _original_chat = _original_json = _original_search = _original_configured = None
    set_script()
    forget_calls()
