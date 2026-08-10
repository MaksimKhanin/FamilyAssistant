"""Thin client for an OpenAI-compatible Chat Completions endpoint.

Deliberately spoken over plain HTTP instead of a vendor SDK: the same code then
works against OpenAI, a local Ollama / vLLM / LM Studio server, or anything else
that implements `/chat/completions` — which is exactly the choice the «Модель и
знания» screen offers the family (локальная 8B / облачная большая / гибрид).

The client knows nothing about the assistant's domain. It sends messages and tool
schemas, and returns either text or the tool calls the model asked for.
"""
import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("llm")


#: Как попросить модель не «размышлять». Единого стандарта нет, поэтому шлём обе
#: распространённые формы сразу: `reasoning_effort` понимают OpenAI-подобные
#: провайдеры, `chat_template_kwargs.enable_thinking` — vLLM/SGLang и Qwen-подобные.
#: Кто не понял ни одной, ответит 400 — тогда запрос повторяется без них (см. `_post`).
REASONING_PRESETS = {
    "off": {"reasoning_effort": "minimal", "chat_template_kwargs": {"enable_thinking": False}},
    "low": {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high": {"reasoning_effort": "high"},
}


class LLMUnavailable(RuntimeError):
    """The model could not be reached or is not configured.

    Callers are expected to degrade gracefully — the assistant says it cannot
    think right now rather than crashing the chat.
    """


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)


def image_part(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    """Content part for a multimodal user message."""
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


class LLMClient:
    def __init__(self, cfg=None):
        self.cfg = cfg or settings.llm

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[dict] = None,
    ) -> LLMResponse:
        if self.cfg.stub:
            from app.agent import stub
            return stub.chat(messages, tools)
        if not self.configured:
            raise LLMUnavailable("LLM не сконфигурирован: задайте LLM_BASE_URL и LLM_MODEL")

        payload: Dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        return self._parse(self._post(payload, self._tuning()))

    def _tuning(self) -> Dict[str, Any]:
        """Поля, управляющие режимом размышления, — их провайдер может и не знать."""
        tuning = dict(REASONING_PRESETS.get(self.cfg.reasoning, {}))
        tuning.update(self.cfg.extra_body or {})
        return tuning

    def _post(self, payload: Dict[str, Any], tuning: Dict[str, Any]) -> dict:
        """Запрос к модели. На 400 с необязательными полями — повтор без них.

        Управление размышлением у каждого провайдера своё, и строгие серверы
        отвечают 400 на незнакомое поле. Вместо того чтобы требовать от хозяина
        дома знать диалект своей модели, пробуем с подсказками и молча
        откатываемся к чистому запросу.
        """
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"

        try:
            response = httpx.post(url, json={**payload, **tuning}, headers=headers,
                                  timeout=self.cfg.request_timeout)
            if response.status_code == 400 and tuning:
                logger.warning(f"Провайдер не принял {sorted(tuning)} — повторяю без них. "
                               f"Задайте LLM_REASONING= (пусто) или LLM_EXTRA_BODY под свою модель.")
                response = httpx.post(url, json=payload, headers=headers,
                                      timeout=self.cfg.request_timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:400]
            logger.error(f"Модель ответила ошибкой {e.response.status_code}: {body}")
            raise LLMUnavailable(f"Модель ответила ошибкой {e.response.status_code}") from e
        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"Не удалось обратиться к модели: {e}")
            raise LLMUnavailable("Модель недоступна") from e

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError):
            raise LLMUnavailable("Модель вернула пустой ответ")

        message = choice.get("message") or {}
        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except ValueError:
                    logger.warning(f"Модель прислала нечитаемые аргументы для {function.get('name')}")
                    arguments = {}
            calls.append(ToolCall(id=raw.get("id") or function.get("name", "call"),
                                  name=function.get("name", ""),
                                  arguments=arguments if isinstance(arguments, dict) else {}))

        return LLMResponse(
            content=(message.get("content") or "").strip(),
            tool_calls=calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage") or {},
        )

    def json_completion(
        self,
        system: str,
        user_content,
        model: Optional[str] = None,
        max_tokens: int = 600,
    ) -> dict:
        """Ask the model for a single JSON object and parse it.

        Used by the vision estimators. `response_format` is sent as a hint but not
        relied upon — plenty of OpenAI-compatible servers ignore it, so the answer
        is also unwrapped from ```json fences before parsing.
        """
        if self.cfg.stub:
            from app.agent import stub
            return stub.json_completion(system, user_content)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        response = self.chat(
            messages,
            model=model,
            temperature=0.1,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return _parse_json_object(response.content)


def _parse_json_object(text: str) -> dict:
    """Extract the outermost JSON object, tolerating ```json fences and stray prose."""
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise LLMUnavailable("Модель вернула не JSON")
    try:
        parsed = json.loads(raw[start:end + 1])
    except ValueError as e:
        raise LLMUnavailable("Модель вернула нечитаемый JSON") from e
    if not isinstance(parsed, dict):
        raise LLMUnavailable("Модель вернула не объект JSON")
    return parsed


client = LLMClient()
