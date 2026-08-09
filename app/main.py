"""FastAPI entrypoint.

    uvicorn app.main:app

The shell — auth, database, templates, navigation, the agent layer — lives in
`core` and `agent`. Every feature is a module under `app/modules/` that the app
assembles itself from at startup. Adding one does not touch this file.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.auth import NotAuthenticatedException
from app.core.config import settings
from app.core.db import create_all
from app.core.events import bus
from app.core.logging import get_logger
from app.core.templating import render
from app.modules import load_modules
from app.web import routes_auth, routes_chat, routes_dashboard, routes_onboarding, routes_settings

logger = get_logger("app")

@asynccontextmanager
async def lifespan(_: FastAPI):
    Path(settings.media_root).mkdir(parents=True, exist_ok=True)
    create_all()
    bus.start()
    logger.info("Семейный ассистент запущен: модули — "
                + ", ".join(m.name for m in load_modules()))
    yield


app = FastAPI(title="Семейный ассистент", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.exception_handler(NotAuthenticatedException)
def not_authenticated_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse("/login", status_code=303)


static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(routes_auth.router)
app.include_router(routes_dashboard.router)
app.include_router(routes_chat.router)
app.include_router(routes_settings.router)
app.include_router(routes_onboarding.router)

for module in load_modules():
    for router in module.routers:
        app.include_router(router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok", "modules": [m.name for m in load_modules()]}


@app.exception_handler(404)
def not_found(request: Request, exc):
    if request.url.path.startswith(("/api/", "/static/")):
        return HTMLResponse("Not found", status_code=404)
    return render(request, "not_found.html", {"request": request}, status_code=404)
