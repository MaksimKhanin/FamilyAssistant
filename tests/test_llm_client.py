"""Клиент модели: режим размышления и запасной путь для строгих провайдеров."""
import httpx
import pytest

from app.agent.llm import LLMClient, LLMUnavailable
from app.core.config import LLMSettings

ANSWER = {"choices": [{"message": {"content": "Здравствуйте."}, "finish_reason": "stop"}]}


def settings(**overrides) -> LLMSettings:
    base = dict(base_url="http://model.local/v1", api_key="k", model="test",
                vision_model="test", temperature=0.3, max_tokens=512, request_timeout=5)
    base.update(overrides)
    return LLMSettings(**base)


class Recorder(list):
    """Список отправленных запросов; в `responses` кладутся ответы по порядку."""

    def __init__(self):
        super().__init__()
        self.responses = []


@pytest.fixture
def calls(monkeypatch):
    """Перехватывает запросы к модели и отдаёт заранее заданные ответы."""
    sent = Recorder()

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        status, body = sent.responses.pop(0) if sent.responses else (200, ANSWER)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_reasoning_is_off_by_default(calls):
    """Ассистент маршрутизирует в инструменты — размышление здесь только тормозит."""
    LLMClient(settings()).chat([{"role": "user", "content": "привет"}])

    assert calls[0]["reasoning_effort"] == "minimal"
    assert calls[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_reasoning_can_be_raised(calls):
    LLMClient(settings(reasoning="high")).chat([{"role": "user", "content": "привет"}])

    assert calls[0]["reasoning_effort"] == "high"
    assert "chat_template_kwargs" not in calls[0]


def test_empty_setting_leaves_the_request_untouched(calls):
    """Пусто — значит не вмешиваться: модель решает сама."""
    LLMClient(settings(reasoning="")).chat([{"role": "user", "content": "привет"}])

    assert "reasoning_effort" not in calls[0]
    assert "chat_template_kwargs" not in calls[0]


def test_extra_body_wins_over_the_preset(calls):
    client = LLMClient(settings(extra_body={"reasoning_effort": "low", "top_p": 0.9}))
    client.chat([{"role": "user", "content": "привет"}])

    assert calls[0]["reasoning_effort"] == "low"
    assert calls[0]["top_p"] == 0.9


def test_a_strict_provider_gets_a_clean_retry(calls):
    """Сервер, не знающий этих полей, отвечает 400 — повторяем без них."""
    calls.responses.append((400, {"error": "unknown field reasoning_effort"}))

    response = LLMClient(settings()).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 2
    assert "reasoning_effort" in calls[0]
    assert "reasoning_effort" not in calls[1]
    assert "chat_template_kwargs" not in calls[1]
    assert response.content == "Здравствуйте."


def test_a_real_bad_request_is_not_retried_forever(calls):
    """Если и чистый запрос отвергнут — это уже настоящая ошибка."""
    calls.responses.extend([(400, {"error": "bad"}), (400, {"error": "bad"})])

    with pytest.raises(LLMUnavailable):
        LLMClient(settings()).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 2


def test_nothing_is_retried_when_there_was_nothing_to_strip(calls):
    calls.responses.append((400, {"error": "bad"}))

    with pytest.raises(LLMUnavailable):
        LLMClient(settings(reasoning="")).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 1


def test_tuning_reaches_json_helpers_too(calls):
    """Оценка блюда и разбор события — такие же быстрые пути, как чат."""
    calls.responses.append((200, {"choices": [{"message": {"content": '{"kcal": 320}'}}]}))

    result = LLMClient(settings()).json_completion("система", "овсянка")

    assert calls[0]["reasoning_effort"] == "minimal"
    assert result == {"kcal": 320}
