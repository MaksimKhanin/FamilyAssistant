"""Журнал стенда: что происходило на сервере, пока сценарий шёл снаружи.

Снаружи виден только ответ — код, HTML, JSON. Всё, из-за чего сценарий на самом
деле сломался, остаётся внутри: предупреждение в логе, исключение в роуте,
прогон агента, который не тот инструмент выбрал, уведомление, которое некому
показать. Журнал поднимает это наверх и складывает в кольцо, из которого
сценарий забирает всё разом — и по номеру шага, а не по времени, чтобы не
угадывать, какая строчка лога чьей была.

Колец три, и они про разное:

  * `requests` — один запрос: метод, адрес, код, время, кто вошёл, какие прогоны
    агента он завёл, какие предупреждения выписал и каким исключением упал;
  * `messages` — то, что ассистент сказал сам: события шины, из которых растут
    push-уведомления. Снаружи их не видно вовсе, а сценарию «напомнил в 9:00» —
    это и есть проверяемый результат;
  * `warnings` живут внутри записи запроса, а не отдельно: предупреждение без
    запроса, который его выписал, ничего не объясняет.

Всё это включается вместе со стендом и только с ним (`app/testkit/__init__.py`):
без ключа кольца пустые, обработчик логов не поставлен, middleware не висит.
"""
import itertools
import logging
import traceback
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import List, Optional
from urllib.parse import unquote

from app.core.events import ACTION_PENDING, AGENT_MESSAGE, bus

#: Сколько запросов помнит кольцо. Сценарий длиной в сотню шагов должен
#: помещаться целиком: журнал читают после прогона, а не по ходу.
KEEP_REQUESTS = 500
KEEP_MESSAGES = 200

#: Предупреждений на один запрос. Ограничение от зацикленного кода: один
#: запрос, выписывающий тысячу строк, не должен вытеснить весь журнал.
WARNINGS_PER_REQUEST = 50

#: Адреса, которые журнал о себе не пишет: обращения самого стенда и статика.
#: Иначе чтение журнала добавляло бы в журнал запись о чтении журнала.
SKIP_PREFIXES = ("/api/testkit/", "/static/", "/favicon.ico")

_requests: deque = deque(maxlen=KEEP_REQUESTS)
_messages: deque = deque(maxlen=KEEP_MESSAGES)
#: Сквозная нумерация записей: и запросы, и сообщения шины считаются одним
#: счётчиком, поэтому «всё, что случилось после шага» — это одно сравнение с
#: числом, а не сверка времён двух колец.
_numbers = itertools.count(1)
_last_no = 0


def _next_no() -> int:
    global _last_no
    _last_no = next(_numbers)
    return _last_no


#: Куда складывать предупреждения прямо сейчас. Список кладётся в контекст до
#: того, как начнётся работа: middleware Starlette уводит роут в отдельную
#: задачу, но контекст в неё копируется, и список остаётся тем же объектом.
_sink: ContextVar[Optional[list]] = ContextVar("testkit_log_sink", default=None)

#: Чей это шаг — приходит заголовком `X-Testkit-Step` от бегунка сценариев.
#: Без него запись пришлось бы искать по времени, а времени в журнале много.
_step: ContextVar[str] = ContextVar("testkit_step", default="")

_installed = False


class _Sink(logging.Handler):
    """Обработчик логов, который пишет не в поток, а в текущий запрос."""

    def emit(self, record: logging.LogRecord):
        bucket = _sink.get()
        if bucket is None or len(bucket) >= WARNINGS_PER_REQUEST:
            return
        entry = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["traceback"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
        bucket.append(entry)


@contextmanager
def capture():
    """Собрать предупреждения, выписанные внутри этого блока.

    Нужен и middleware, и ручке `say`: та зовёт агента напрямую, минуя HTTP,
    и без своего сбора её предупреждения ушли бы в общий лог и пропали.
    """
    bucket: List[dict] = []
    token = _sink.set(bucket)
    try:
        yield bucket
    finally:
        _sink.reset(token)


def install():
    """Поставить обработчик логов и подписаться на шину. Идемпотентно."""
    global _installed
    if _installed:
        return
    handler = _Sink()
    handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler)
    bus.subscribe(AGENT_MESSAGE, lambda payload: _remember_message("agent.message", payload))
    bus.subscribe(ACTION_PENDING, lambda payload: _remember_message("action.pending", payload))
    _installed = True


def _remember_message(topic: str, payload: dict):
    # Подписчик шины не имеет права падать: за ним стоит доставка уведомлений.
    try:
        _messages.append({
            "no": _next_no(),
            "at": datetime.utcnow().isoformat(),
            "step": _step.get(),
            "topic": topic,
            "payload": payload,
        })
    except Exception:                                        # noqa: BLE001
        pass


def add_request(record: dict) -> dict:
    record["no"] = _next_no()
    _requests.append(record)
    return record


def requests(since: int = 0, limit: int = 100, path: str = "", step: str = "",
             only_bad: bool = False) -> List[dict]:
    """Записи журнала: свежие в конце, как их читает человек.

    `since` — номер, после которого читать: сценарий берёт номер до шага и
    после шага получает ровно то, что этот шаг наделал.
    """
    rows = [r for r in _requests if r["no"] > since]
    if path:
        rows = [r for r in rows if path in r["path"]]
    if step:
        rows = [r for r in rows if r.get("step") == step]
    if only_bad:
        rows = [r for r in rows if r["status"] >= 400 or r.get("error") or r.get("warnings")]
    return rows[-limit:]


def messages(since: int = 0, limit: int = 100) -> List[dict]:
    return [m for m in _messages if m["no"] > since][-limit:]


def cursor() -> int:
    """Последний выданный номер: всё, что придёт дальше, будет больше него."""
    return _last_no


def clear():
    _requests.clear()
    _messages.clear()


def set_step(name: str):
    # Имя шага приезжает заголовком и потому закодировано: заголовки — латиница,
    # а шаги сценария названы словами.
    _step.set(unquote(name or ""))


def current_step() -> str:
    return _step.get()
