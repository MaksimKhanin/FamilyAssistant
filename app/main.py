"""FastAPI entrypoint.

    uvicorn app.main:app

The shell — auth, database, templates, navigation, the agent layer — lives in
`core` and `agent`. Every feature is a module under `app/modules/` that the app
assembles itself from at startup. Adding one does not touch this file.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core import push
from app.core.auth import NotAuthenticatedException
from app.core.bootstrap import ensure_admin
from app.core.config import settings
from app.core.db import session_scope, upgrade_schema
from app.core.events import AGENT_MESSAGE, bus
from app.core.logging import get_logger
from app.core.templating import render
from app.modules import load_modules
from app.web import (
    routes_auth, routes_chat, routes_dashboard, routes_invite, routes_onboarding,
    routes_push, routes_settings, routes_traces,
)

logger = get_logger("app")

@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    upgrade_schema()

    # Глава семьи заводится из окружения при первом старте; дальше учётные записи
    # он нарезает в панели. Вызов идемпотентный — на непустой базе ничего не делает.
    with session_scope() as db:
        ensure_admin(db)

    # Уведомления рассылает только веб-процесс: при работе через Redis событие
    # приходит во все процессы, и подпишись на него каждый — семья получила бы
    # по три одинаковых сообщения на каждую тревогу.
    bus.subscribe(AGENT_MESSAGE, push.handle_agent_message)
    bus.start()

    if not push.configured():
        logger.warning("VAPID-ключи не заданы — уведомления на телефоны не пойдут "
                       "(python -m scripts.vapid_keys)")
    logger.info("Семейный ассистент запущен: модули — "
                + ", ".join(m.name for m in load_modules()))
    yield


app = FastAPI(title="Семейный ассистент", docs_url=None, redoc_url=None, lifespan=lifespan)

# Экран — это целый HTML-документ (ADR-0001), и по мобильной сети он летает
# только сжатым. Медиа с камер middleware не трогает сам: jpeg, video/* и
# 206-ответы (перемотка видео) в его списке исключений.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.exception_handler(NotAuthenticatedException)
def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse("/login", status_code=303)


class CachedStaticFiles(StaticFiles):
    """Статика с годовым кешем.

    Ссылки на неё идут через `static_url` (см. core/templating.py) и несут хэш
    содержимого в query: файл поменялся — поменялся URL, поэтому браузеру можно
    не переспрашивать вовсе. Без этого каждый холодный старт PWA шёл на сервер
    за каждым файлом.
    """
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


static_dir = Path(__file__).parent / "static"
app.mount("/static", CachedStaticFiles(directory=str(static_dir)), name="static")

app.include_router(routes_auth.router)
app.include_router(routes_invite.router)
app.include_router(routes_dashboard.router)
app.include_router(routes_chat.router)
app.include_router(routes_push.router)
app.include_router(routes_settings.router)
app.include_router(routes_traces.router)
app.include_router(routes_onboarding.router)

for module in load_modules():
    for router in module.routers:
        app.include_router(router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "modules": [m.name for m in load_modules()]}


# Service worker обязан отдаваться из корня: со /static/ его область видимости
# ограничилась бы этой папкой, и он не смог бы управлять страницами приложения.
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(static_dir / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(static_dir / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(static_dir / "icons" / "favicon-32.png", media_type="image/png")


@app.exception_handler(404)
def not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/static/")):
        return HTMLResponse("Not found", status_code=404)
    return render(request, "not_found.html", {"request": request}, status_code=404)
