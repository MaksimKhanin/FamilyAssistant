"""Клиент модели: режим размышления и запасной путь для строгих провайдеров."""
import httpx
import pytest

from app.agent.llm import REASONING_HEADROOM, LLMClient, LLMUnavailable
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

    assert calls[0]["reasoning_effort"] == "none"
    assert calls[0]["reasoning"] == {"effort": "none"}
    assert calls[0]["enable_thinking"] is False
    assert calls[0]["chat_template_kwargs"] == {"enable_thinking": False}


def test_reasoning_can_be_raised(calls):
    LLMClient(settings(reasoning="high")).chat([{"role": "user", "content": "привет"}])

    assert calls[0]["reasoning_effort"] == "high"
    assert "enable_thinking" not in calls[0]
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


def test_extra_body_survives_every_rung(calls):
    """Хозяин дома подобрал поля под свою модель — их нельзя терять при откате."""
    calls.responses.extend([(400, {"error": "bad"})] * 2)
    client = LLMClient(settings(extra_body={"top_p": 0.9}))

    client.chat([{"role": "user", "content": "привет"}])

    assert [call["top_p"] for call in calls] == [0.9, 0.9, 0.9]
    assert "reasoning_effort" not in calls[-1]


def test_a_strict_provider_gets_the_next_rung(calls):
    """OpenAI не знает значения `none`, но знает `minimal` — на 400 пробуем его."""
    calls.responses.append((400, {"error": "unsupported value: reasoning_effort=none"}))

    response = LLMClient(settings()).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 2
    assert calls[0]["reasoning_effort"] == "none"
    assert calls[1]["reasoning_effort"] == "minimal"
    assert "enable_thinking" not in calls[1]
    assert response.content == "Здравствуйте."


def test_the_last_rung_is_a_clean_request(calls):
    """Не понял ни одной формы — уходит чистый запрос, лишь бы ассистент ответил."""
    calls.responses.extend([(400, {"error": "bad"}), (400, {"error": "bad"})])

    response = LLMClient(settings()).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 3
    assert "reasoning_effort" not in calls[2]
    assert "reasoning" not in calls[2]
    assert "chat_template_kwargs" not in calls[2]
    assert response.content == "Здравствуйте."


def test_a_real_bad_request_is_not_retried_forever(calls):
    """Если и чистый запрос отвергнут — это уже настоящая ошибка."""
    calls.responses.extend([(400, {"error": "bad"})] * 3)

    with pytest.raises(LLMUnavailable):
        LLMClient(settings()).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 3


def test_nothing_is_retried_when_there_was_nothing_to_strip(calls):
    calls.responses.append((400, {"error": "bad"}))

    with pytest.raises(LLMUnavailable):
        LLMClient(settings(reasoning="")).chat([{"role": "user", "content": "привет"}])

    assert len(calls) == 1


def test_tuning_reaches_json_helpers_too(calls):
    """Оценка блюда и разбор события — такие же быстрые пути, как чат."""
    calls.responses.append((200, {"choices": [{"message": {"content": '{"kcal": 320}'}}]}))

    result = LLMClient(settings()).json_completion("система", "овсянка")

    assert calls[0]["reasoning_effort"] == "none"
    assert result == {"kcal": 320}


def test_the_chat_stays_silent_while_estimates_think(calls):
    """Две ручки независимы: чат остаётся быстрым, даже если оценке разрешили думать."""
    client = LLMClient(settings(reasoning="off", reasoning_estimate="high"))

    client.chat([{"role": "user", "content": "привет"}])
    calls.responses.append((200, {"choices": [{"message": {"content": "{}"}}]}))
    client.json_completion("система", "овсянка")

    assert calls[0]["reasoning_effort"] == "none"
    assert calls[1]["reasoning_effort"] == "high"


def test_thinking_gets_its_own_token_budget(calls):
    """Мысли тратят бюджет ответа: без запаса модель успевает подумать и умолкнуть."""
    calls.responses.append((200, {"choices": [{"message": {"content": "{}"}}]}))
    LLMClient(settings(reasoning_estimate="low")).json_completion("система", "овсянка",
                                                                  max_tokens=600)

    assert calls[0]["max_tokens"] == 600 + REASONING_HEADROOM


def test_a_truncated_answer_complains_about_the_limit(calls):
    """«Модель вернула не JSON» уводит не туда: дело не в модели, а в лимите."""
    calls.responses.append((200, {"choices": [{"message": {"content": ""},
                                               "finish_reason": "length"}]}))

    with pytest.raises(LLMUnavailable, match="токен"):
        LLMClient(settings(reasoning_estimate="low")).json_completion("система", "овсянка")
