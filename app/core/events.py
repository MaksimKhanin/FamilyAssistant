"""Event Bus.

Modules publish facts («камера увидела человека», «записан приём пищи») and other
parts of the system react — the security module notifies Telegram, the nutrition
module recalculates the daily balance, a future module does something else. The
publisher never knows who listens.

Two backends behind one API:

  * in-process (default) — handlers run synchronously in the caller's thread;
  * Redis pub/sub (when REDIS_URL is set) — the same event also reaches the other
    processes (web app ↔ Telegram bot ↔ scheduler), each of which dispatches it to
    its own local handlers.

Handlers must not raise: a failing subscriber is logged and skipped so one broken
listener cannot take down the publisher.
"""
import json
import threading
from collections import defaultdict
from typing import Callable, Dict, List

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("events")

REDIS_CHANNEL = "family_assistant:events"

# --- topics ---------------------------------------------------------------
SECURITY_EVENT_CREATED = "security.event.created"
SECURITY_ANOMALY = "security.anomaly"
MEAL_LOGGED = "nutrition.meal.logged"
MEAL_CONFIRMED = "nutrition.meal.confirmed"
ACTIVITY_LOGGED = "nutrition.activity.logged"
NOTE_CREATED = "memory.note.created"
AGENT_MESSAGE = "agent.message"           # агент хочет что-то сказать человеку
ACTION_PENDING = "agent.action.pending"   # действие ждёт подтверждения


class EventBus:
    def __init__(self):
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)
        self._redis = None
        self._listener: threading.Thread = None
        self._lock = threading.Lock()

    # -- subscription ------------------------------------------------------
    def subscribe(self, topic: str, handler: Callable[[dict], None]):
        with self._lock:
            self._handlers[topic].append(handler)
        return handler

    def on(self, topic: str):
        """Decorator form: @bus.on(SECURITY_ANOMALY)."""
        def wrapper(fn):
            self.subscribe(topic, fn)
            return fn
        return wrapper

    # -- publishing --------------------------------------------------------
    def publish(self, topic: str, payload: dict = None):
        payload = payload or {}
        if self._redis is not None:
            try:
                self._redis.publish(REDIS_CHANNEL, json.dumps({"topic": topic, "payload": payload}))
                return  # локальные обработчики отработают, когда событие вернётся из Redis
            except Exception:
                logger.exception("Не удалось опубликовать событие в Redis, обрабатываю локально")
        self.dispatch(topic, payload)

    def dispatch(self, topic: str, payload: dict):
        for handler in list(self._handlers.get(topic, ())):
            try:
                handler(payload)
            except Exception:
                logger.exception(f"Обработчик события {topic} упал: {getattr(handler, '__name__', handler)}")

    # -- redis backend -----------------------------------------------------
    def start(self):
        """Connect the cross-process backend if configured. Safe to call twice."""
        if not settings.redis_url or self._redis is not None:
            return
        try:
            import redis  # импортируется только когда Redis реально настроен
        except ImportError:
            logger.warning("REDIS_URL задан, но пакет redis не установлен — шина работает в одном процессе")
            return
        try:
            client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            client.ping()
        except Exception:
            logger.exception("Redis недоступен — шина событий работает в одном процессе")
            return

        self._redis = client
        self._listener = threading.Thread(target=self._listen, name="event-bus", daemon=True)
        self._listener.start()
        logger.info("Шина событий подключена к Redis")

    def _listen(self):
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(REDIS_CHANNEL)
        for message in pubsub.listen():
            try:
                data = json.loads(message["data"])
            except (ValueError, KeyError, TypeError):
                logger.warning("Получено нечитаемое событие из Redis, пропускаю")
                continue
            self.dispatch(data.get("topic", ""), data.get("payload") or {})


bus = EventBus()
