"""One logging setup for the server: панель, бот и планировщик."""
import logging
import os
import sys

_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_configured = False


def _configure_once():
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_LEVEL)
    # Библиотеки, которые любят шуметь на каждый HTTP-запрос.
    for noisy in ("httpx", "httpcore", "urllib3", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_once()
    return logging.getLogger(name)
