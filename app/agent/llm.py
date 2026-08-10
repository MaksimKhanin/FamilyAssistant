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
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.agent import tracing
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("llm")


def _trace(body: dict, response=None, started: float = None,
           status: str = "ok", error: str = None):
    """Отдать обмен с моделью писарю трейсов, если сейчас идёт прогон агента."""
    recorder = tracing.current()
    if recorder is None:
        return
    try:
        answer = response.json() if response is not None else {"ошибка": error}
    except ValueError:
        answer = {"код": response.status_code, "тело": response.text[:2000]}
    if response is not None and response.status_code >= 400:
        answer = {"код": response.status_code, "тело": response.text[:2000]}
        status = status if status == "retried" else "failed"
    recorder.llm(
        body, answer, status=status,
        usage=(answer.get("usage") if isinstance(answer, dict) else None) or {},
        duration_ms=int((time.monotonic() - started) * 1000) if started else 0,
    )


#: Как попросить модель не «размышлять». Единого стандарта нет, и значения у
#: провайдеров расходятся: OpenRouter и LiteLLM понимают `reasoning_effort: none`
#: (и вложенный `reasoning.effort`), OpenAI такого значения не знает, но знает
#: `minimal`, vLLM/SGLang слушают `chat_template_kwargs.enable_thinking`, а часть
#: сборок Qwen3 и DashScope — голый `enable_thinking`.
#:
#: Поэтому под каждый режим — лесенка попыток: первый вариант самый широкий, и
#: если провайдер ответил на него 400, пробуется следующий, а в конце — чистый
#: запрос вовсе без этих полей (см. `_post`). Хозяину дома не нужно знать диалект
#: своей модели.
#:
#: Внимание: молчаливое «принял и проигнорировал» — обычное дело. Проверять
#: результат надо не кодом ответа, а `usage.completion_tokens`: у думающей модели
#: их сотни там, где хватило бы трёх.
REASONING_PRESETS = {
    "off": [
        {
            "reasoning_effort": "none",
            "reasoning": {"effort": "none"},
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
        # OpenAI: `none` не знает, зато у reasoning-моделей есть `minimal`.
        {"reasoning_effort": "minimal"},
    ],
    "low": [{"reasoning_effort": "low"}],
    "medium": [{"reasoning_effort": "medium"}],
    "high": [{"reasoning_effort": "high"}],
}

#: Сколько токенов добавить к ответу, когда модели разрешено размышлять. Мысли
#: расходуют тот же бюджет, что и сам ответ, — без запаса на JSON места не остаётся.
REASONING_HEADROOM = 1200


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
        reasoning: Optional[str] = None,
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

        return self._parse(self._post(payload, self._tuning(reasoning)))

    def _tuning(self, reasoning: Optional[str] = None) -> List[Dict[str, Any]]:
        """Лесенка попыток управления размышлением — от широкой к пустой.

        `reasoning` задаёт режим для одного вызова: чат ассистента маршрутизирует
        в инструменты и думать не должен, а оценка КБЖУ — как раз должна.

        `LLM_EXTRA_BODY` — последнее слово хозяина дома: он перекрывает пресет и
        едет во всех попытках, включая ту, где от пресета не осталось ничего.
        """
        mode = self.cfg.reasoning if reasoning is None else reasoning
        extra = dict(self.cfg.extra_body or {})
        ladder: List[Dict[str, Any]] = []
        for preset in REASONING_PRESETS.get(mode, []):
            variant = {**preset, **extra}
            # Пустышки и повторы отбрасываем: незачем дважды слать один и тот же запрос.
            if variant and variant != extra and variant not in ladder:
                ladder.append(variant)
        ladder.append(extra)  # последняя ступень — только то, что задал хозяин дома
        return ladder

    def _post(self, payload: Dict[str, Any], ladder: List[Dict[str, Any]]) -> dict:
        """Запрос к модели. На 400 — следующая попытка из лесенки, вплоть до чистой.

        Управление размышлением у каждого провайдера своё, и строгие серверы
        отвечают 400 на незнакомое поле или незнакомое значение. Вместо того чтобы
        требовать от хозяина дома знать диалект своей модели, спускаемся по
        лесенке и в крайнем случае откатываемся к чистому запросу.
        """
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"

        try:
            for number, tuning in enumerate(ladder):
                body = {**payload, **tuning}
                started = time.monotonic()
                response = httpx.post(url, json=body, headers=headers,
                                      timeout=self.cfg.request_timeout)
                last = number == len(ladder) - 1
                # Пишем каждую попытку: лесенка — как раз то, что хочется увидеть
                # в трейсе, когда провайдер молча игнорирует половину полей.
                _trace(body, response, started, status="ok" if response.status_code != 400 or last
                       else "retried")
                if response.status_code != 400 or last:
                    break
                logger.warning(f"Провайдер не принял {sorted(tuning)} — пробую дальше. "
                               f"Задайте LLM_REASONING= (пусто) или LLM_EXTRA_BODY под свою модель.")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            text = e.response.text[:400]
            logger.error(f"Модель ответила ошибкой {e.response.status_code}: {text}")
            raise LLMUnavailable(f"Модель ответила ошибкой {e.response.status_code}") from e
        except (httpx.HTTPError, ValueError) as e:
            logger.error(f"Не удалось обратиться к модели: {e}")
            _trace(payload, None, None, status="failed", error=str(e))
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
        reasoning: Optional[str] = None,
    ) -> dict:
        """Ask the model for a single JSON object and parse it.

        Used by the vision estimators. `response_format` is sent as a hint but not
        relied upon — plenty of OpenAI-compatible servers ignore it, so the answer
        is also unwrapped from ```json fences before parsing.

        Размышление здесь своё (`LLM_REASONING_ESTIMATE`, по умолчанию `low`):
        прикинуть вес порции и сложить калории — ровно та работа, ради которой
        оно и нужно, в отличие от выбора инструмента в чате.
        """
        if self.cfg.stub:
            from app.agent import stub
            return stub.json_completion(system, user_content)

        mode = self.cfg.reasoning_estimate if reasoning is None else reasoning
        if mode not in ("", "off"):
            # Размышление тратит тот же бюджет, что и ответ. Без запаса модель
            # успевает только подумать: приходит finish_reason=length с пустым
            # content, и JSON не рождается вовсе.
            max_tokens += REASONING_HEADROOM

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
            reasoning=mode,
        )
        if response.finish_reason == "length" and not response.content:
            # Отдельная жалоба, потому что «модель вернула не JSON» уводит не туда:
            # ответа нет не потому, что модель глупая, а потому что ей не хватило места.
            logger.error(f"Ответ не поместился в max_tokens={max_tokens}. Поднимите лимит "
                         f"или поставьте LLM_REASONING_ESTIMATE=off.")
            raise LLMUnavailable("Модель не уложилась в отведённые токены")
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
