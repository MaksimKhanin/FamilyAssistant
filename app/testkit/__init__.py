"""Стенд — служебный ход в панель, которым сценарии проходят снаружи.

Панель проверяется людьми и тестами, но между ними есть щель. Тест знает, что
проверяет, и потому не найдёт того, чего не ждал; человек находит, но не может
пройти двадцать сценариев подряд и разобрать, что именно сломалось внутри.
Стенд закрывает щель: сценарий идёт снаружи обычными вызовами, а изнутри
поднимаются трейсы, предупреждения и снимок данных — по ним и видно, что
ассистент сказал «записала», ничего не записав.

Три части, у каждой свой файл:

  * `director` — что «решит» модель на этом ходу: очередь ответов вместо живой
    модели, ради повторяемости и ради редких случаев, которых не дождаться;
  * `journal` — что происходило на сервере: запросы, предупреждения, исключения,
    сообщения шины;
  * `snapshot` + `fixture` — известная база до сценария и снимок данных после.

Включается одним ключом (`TESTKIT_TOKEN`) и без него не существует: роутер не
подключён, middleware не висит, обработчик логов не поставлен, модель не
подменена. Ключ короче двадцати знаков стенд не принимает — эти ручки ходят мимо
входа и мимо ролей, и подобрать ключ к ним не должно быть проще, чем пароль.

Порядок в бою: не включать. Стенд для локального прогона и для отдельно стоящей
проверочной среды, где не жалко базы, — `reset` сносит её целиком.
"""
import time
import traceback

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.logging import get_logger
from app.testkit import journal

logger = get_logger("testkit")

#: Короче — не ключ. Стенд открывает чужие данные и сносит базу; ключ, который
#: подбирается перебором, здесь равнозначен его отсутствию.
MIN_TOKEN_LENGTH = 20


def enabled() -> bool:
    return len(settings.testkit_token) >= MIN_TOKEN_LENGTH


def install(app):
    """Подключить стенд к приложению, если ключ на месте.

    Зовётся из `app/main.py` один раз при сборке приложения. Всё, что стенд
    делает с чужим кодом — middleware, обработчик логов, подмена модели, —
    происходит здесь и только здесь, чтобы выключенный стенд ничего не менял.
    """
    if not settings.testkit_token:
        return False
    if not enabled():
        logger.warning(f"TESTKIT_TOKEN короче {MIN_TOKEN_LENGTH} знаков — стенд не включён")
        return False

    from app.testkit import director, routes

    journal.install()
    director.install()
    app.middleware("http")(_journal_middleware)
    app.include_router(routes.router)
    logger.warning("Стенд включён: /api/testkit открыт по ключу TESTKIT_TOKEN. "
                   "В бою так быть не должно — ручки ходят мимо входа и мимо ролей.")
    return True


async def _journal_middleware(request, call_next):
    """Каждый запрос — строкой в журнале, вместе с тем, что он наделал внутри.

    Прогоны агента находятся сравнением номеров до и после: связать запрос с
    трейсом иначе нечем — прогон открывается глубоко внутри, в `runtime`, и
    наружу его номер не возвращается.
    """
    path = request.url.path
    if path.startswith(journal.SKIP_PREFIXES):
        return await call_next(request)

    journal.set_step(request.headers.get("X-Testkit-Step") or journal.current_step())
    started = time.monotonic()
    runs_before = _last_run_id()
    error = None
    status = 500

    with journal.capture() as warnings:
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception as e:                               # noqa: BLE001 — запись и дальше наверх
            error = {"type": type(e).__name__, "message": str(e),
                     "traceback": traceback.format_exc()[-4000:]}
            raise
        finally:
            journal.add_request({
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "step": journal.current_step(),
                "method": request.method,
                "path": path,
                "query": str(request.url.query or ""),
                "status": status,
                "ms": int((time.monotonic() - started) * 1000),
                "runs": _runs_after(runs_before),
                "warnings": list(warnings),
                "error": error,
            })

    return response


def _last_run_id() -> int:
    from app.core.models import AgentRun

    db = SessionLocal()
    try:
        row = db.query(AgentRun.id).order_by(AgentRun.id.desc()).first()
        return row.id if row else 0
    except Exception:                                        # noqa: BLE001 — журнал не мешает работе
        return 0
    finally:
        db.close()


def _runs_after(after_id: int) -> list:
    from app.core.models import AgentRun

    db = SessionLocal()
    try:
        return [row.id for row in
                db.query(AgentRun.id).filter(AgentRun.id > after_id).order_by(AgentRun.id).all()]
    except Exception:                                        # noqa: BLE001
        return []
    finally:
        db.close()
