"""Ручки стенда: пройти сценарий снаружи и увидеть, чем он обернулся внутри.

Все ручки под `/api/testkit` и все за одним ключом. Ключ — единственная защита:
адреса `/api/` панель пропускает мимо входа и мимо ролей (`app/core/roles.py`),
и это правильно для ingest с камер, но означает, что стенд обязан проверять
себя сам. Ключа нет — стенда нет вовсе: роутер не подключается (`__init__.py`).

Ручки делятся на три рода:

  * **делать** — `reset`, `login`, `say`, `confirm`, `tool`, `tick`: то же самое,
    что делает человек в панели, только вызовом, а не нажатием;
  * **смотреть** — `state`, `traces`, `requests`, `messages`, `model/calls`:
    что от этого стало с данными, с моделью и с сервером;
  * **подстраивать** — `model/script`: что «решит» модель на следующем ходу.

Ответ каждой «делающей» ручки уже несёт с собой хвост: предупреждения, которые
сервер выписал за этот ход, и номер журнальной записи, после которой смотреть
дальше. Сценарию не нужно угадывать, что спросить следом.
"""
import base64
import hmac
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.agent import registry, tracing
from app.agent.runtime import AgentReply, agent, approve_action, reject_action, run_tool_directly
from app.core.auth import set_session_cookie
from app.core.clock import local_now
from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.core.models import AgentRun, PendingAction, User
from app.modules import load_modules
from app.testkit import director, fixture, journal, snapshot

logger = get_logger("testkit")

#: Стенда нет в схеме приложения: `/openapi.json` панель отдаёт кому угодно, и
#: перечислять там служебные ручки — значит рассказывать о них всем, кто мимо
#: проходил. Ключ от этого не защищает: знать, что дверь есть, уже достаточно,
#: чтобы начать её пробовать.
router = APIRouter(prefix="/api/testkit", tags=["testkit"], include_in_schema=False)


# --- вход -----------------------------------------------------------------

def guard(request: Request):
    """Ключ стенда — заголовком `X-Testkit-Token` или параметром `token`."""
    given = request.headers.get("X-Testkit-Token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(given, settings.testkit_token):
        raise HTTPException(status_code=401, detail="Стенд: не тот ключ")
    journal.set_step(request.headers.get("X-Testkit-Step") or "")


def _user(db: Session, name: Any) -> User:
    """Человек по логину или по номеру — сценарию удобнее логином."""
    if name is None or name == "":
        raise HTTPException(status_code=400, detail="Не сказано, за кого ходим (user)")
    query = db.query(User)
    row = (query.filter(User.id == int(name)).one_or_none() if str(name).isdigit()
           else query.filter(User.username == str(name)).one_or_none())
    if row is None:
        raise HTTPException(status_code=404, detail=f"Нет такого человека: {name}")
    return row


def _optional_user(db: Session, name: Any) -> Optional[User]:
    return None if name in (None, "", "all") else _user(db, name)


# --- смотреть -------------------------------------------------------------

@router.get("/health", dependencies=[Depends(guard)])
def health(db: Session = Depends(get_db)):
    return {
        "ok": True,
        "modules": [m.name for m in load_modules()],
        "model": {
            "stub": settings.llm.stub,
            "model": settings.llm.model,
            "scripted": {kind: len(rows) for kind, rows in director.script().items()},
        },
        "users": fixture.describe(db)["users"],
        "cursor": journal.cursor(),
    }


@router.get("/routes", dependencies=[Depends(guard)])
def routes(request: Request):
    """Все адреса панели — сценарию, который обходит экраны подряд.

    Экраны отдают модули, и список их адресов нигде не записан заранее: он
    складывается при сборке приложения. Обходчику незачем знать это наизусть.
    """
    rows = [row for row in _walk(request.app.routes)
            if not row["path"].startswith(("/api/testkit", "/static"))]
    unique = {(row["path"], tuple(row["methods"])): row for row in rows}
    return {"routes": sorted(unique.values(), key=lambda r: r["path"])}


def _walk(routes, depth: int = 0) -> List[dict]:
    """Адреса из дерева роутов — с обходом вложенных роутеров.

    FastAPI подключает роутер не разворачивая: в `app.routes` лежит обёртка, а
    сами адреса — внутри неё. Поэтому обход рекурсивный, а не перебор списка.
    """
    found: List[dict] = []
    if depth > 4:
        return found
    for route in routes or []:
        nested = getattr(route, "original_router", None) or getattr(route, "router", None)
        if nested is not None:
            found.extend(_walk(getattr(nested, "routes", []), depth + 1))
            continue
        path = getattr(route, "path", None)
        if path:
            found.append({"path": path, "methods": sorted(getattr(route, "methods", []) or [])})
    return found


@router.get("/state", dependencies=[Depends(guard)])
def state(user: str = None, tables: str = "", limit: int = snapshot.DEFAULT_LIMIT,
          counts: bool = False, db: Session = Depends(get_db)):
    who = _optional_user(db, user)
    only = [name for name in tables.split(",") if name.strip()] if tables else None
    payload = {"user": who.username if who else "all",
               "tables": snapshot.snapshot(db, who, only=only, limit=limit)}
    if counts:
        payload["counts"] = snapshot.counts(db, who)
    return payload


@router.get("/traces", dependencies=[Depends(guard)])
def traces(user: str = None, session: str = None, run_id: int = None, limit: int = 20,
           db: Session = Depends(get_db)):
    """Прогоны агента целиком: промпты, вызовы инструментов, токены, ошибки."""
    who = _optional_user(db, user)
    family_id = who.family_id if who else _any_family(db)
    if family_id is None:
        return {"runs": [], "by_session": [], "by_user": []}
    return tracing.export(db, family_id, user_id=who.id if who else None,
                          session_id=session, run_id=run_id, limit=limit)


def _any_family(db: Session) -> Optional[int]:
    row = db.query(User).first()
    return row.family_id if row else None


@router.get("/requests", dependencies=[Depends(guard)])
def requests(since: int = 0, limit: int = 100, path: str = "", step: str = "",
             only_bad: bool = False):
    return {"cursor": journal.cursor(),
            "requests": journal.requests(since=since, limit=limit, path=path, step=step,
                                         only_bad=only_bad)}


@router.get("/messages", dependencies=[Depends(guard)])
def messages(since: int = 0, limit: int = 100):
    """Что ассистент сказал сам: напоминания, сводки, тревоги с камер."""
    return {"cursor": journal.cursor(), "messages": journal.messages(since=since, limit=limit)}


@router.get("/cursor", dependencies=[Depends(guard)])
def cursor():
    return {"cursor": journal.cursor(), "model_calls": director.count()}


# --- подстраивать ---------------------------------------------------------

@router.post("/model/script", dependencies=[Depends(guard)])
def set_script(payload: Dict[str, Any] = Body(default={})):
    """Очередь ответов модели на ближайшие ходы (см. `app/testkit/director.py`)."""
    director.set_script(chat=payload.get("chat"), json_replies=payload.get("json"))
    if payload.get("forget_calls"):
        director.forget_calls()
    return {"ok": True, "script": director.script()}


@router.get("/model/calls", dependencies=[Depends(guard)])
def model_calls(since: int = 0, limit: int = 50):
    return {"calls": director.calls(since=since, limit=limit)}


# --- делать ---------------------------------------------------------------

@router.post("/reset", dependencies=[Depends(guard)])
def reset(payload: Dict[str, Any] = Body(default={})):
    """Снести базу и собрать стенд заново.

    `confirm: "wipe"` обязателен и написан руками не из вредности: ключ стенда
    может оказаться и на живой базе, а эта ручка стирает её целиком.
    """
    if payload.get("confirm") != "wipe":
        raise HTTPException(status_code=400,
                            detail="Стенд сносит базу целиком — подтвердите: confirm=wipe")
    logger.warning("Стенд: база снесена и собрана заново")
    result = fixture.reset(
        autonomy=int(payload.get("autonomy", 2)),
        traces=bool(payload.get("traces", True)),
        seed=bool(payload.get("seed", True)),
    )
    director.set_script()
    director.forget_calls()
    journal.clear()
    return {"ok": True, **result, "cursor": journal.cursor()}


@router.post("/login", dependencies=[Depends(guard)])
def login(payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    """Кука входа без пароля — чтобы дальше ходить по экранам обычным HTTP.

    Пароль сценарию знать неоткуда и незачем: вход человеком проверяется своим
    сценарием через `/login`, а всем остальным нужен уже вошедший человек.
    """
    user = _user(db, payload.get("user"))
    response = JSONResponse({"ok": True, "user": user.username, "id": user.id,
                             "role": user.role, "home": "/" if user.is_member else "/settings/accounts"})
    set_session_cookie(response, user)
    return response


@router.post("/say", dependencies=[Depends(guard)])
def say(payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    """Реплика человека ассистенту — и всё, что из неё вышло.

    Зовёт то же `agent.respond`, что и экран чата: путь один, разница только в
    том, что ответ отдаётся разобранным, а не разметкой. Исключение не роняет
    ручку — оно возвращается со стеком: сценарий ищет как раз такое.
    """
    user = _user(db, payload.get("user"))
    text = (payload.get("text") or "").strip()
    image = base64.b64decode(payload["image_b64"]) if payload.get("image_b64") else None
    if not text and not image:
        raise HTTPException(status_code=400, detail="Пустая реплика")

    since = journal.cursor()
    last_run = _last_run_id(db, user)
    calls_before = director.count()
    error = None
    reply = AgentReply(text="")

    with journal.capture() as warnings:
        started = local_now()
        try:
            reply = agent.respond(db, user, text, image=image,
                                  channel=payload.get("channel") or "web", subject=user)
        except Exception as e:                               # noqa: BLE001 — ради стека в ответе
            logger.exception("Стенд: ответ ассистента упал")
            error = {"type": type(e).__name__, "message": str(e),
                     "traceback": traceback.format_exc()[-4000:]}

    return {
        "user": user.username,
        "said": text or "(фото)",
        "reply": reply.text,
        "duration_ms": int((local_now() - started).total_seconds() * 1000),
        **reply.to_payload(),
        "pending": _pending(db, user),
        "run": _run_payload(db, user, last_run),
        "model_calls": director.calls(since=calls_before),
        "warnings": warnings,
        "error": error,
        "cursor": journal.cursor(),
        "since": since,
    }


@router.post("/confirm", dependencies=[Depends(guard)])
def confirm(payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    """«Да» или «нет» на подготовленное действие — то же, что кнопка в карточке."""
    user = _user(db, payload.get("user"))
    raw = payload.get("pending_id", "last")
    if raw in (None, "", "last"):
        waiting = _pending(db, user)
        if not waiting:
            raise HTTPException(status_code=404, detail="Нечего подтверждать: нет ждущих действий")
        pending_id = waiting[-1]["id"]
    else:
        pending_id = int(raw)

    decision = (payload.get("decision") or "approve").lower()
    with journal.capture() as warnings:
        if decision in ("approve", "yes", "да"):
            result = approve_action(db, pending_id, user, channel=payload.get("channel") or "web")
        else:
            result = reject_action(db, pending_id, user, channel=payload.get("channel") or "web")

    return {"user": user.username, "pending_id": pending_id, "decision": decision,
            "ok": result.ok, "summary": result.summary, "card": result.card,
            "data": result.data, "pending": _pending(db, user), "warnings": warnings,
            "cursor": journal.cursor()}


@router.post("/tool", dependencies=[Depends(guard)])
def tool(payload: Dict[str, Any] = Body(default={}), db: Session = Depends(get_db)):
    """Вызвать инструмент напрямую, минуя модель.

    Готовит состояние для сценария тем же кодом, которым его меняет ассистент:
    заготовка, положенная в базу руками, проверяет не то, что проверяет сценарий.
    """
    user = _user(db, payload.get("user"))
    name = payload.get("tool") or ""
    if registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"Нет такого инструмента: {name}")
    with journal.capture() as warnings:
        result = run_tool_directly(db, user, name, payload.get("arguments") or {},
                                   mode=payload.get("mode") or "event")
    return {"tool": name, "ok": result.ok, "summary": result.summary, "data": result.data,
            "card": result.card, "warnings": warnings, "cursor": journal.cursor()}


@router.post("/tick", dependencies=[Depends(guard)])
def tick(payload: Dict[str, Any] = Body(default={})):
    """Прогнать минуту расписаний: сводки, напоминания, ротация архива.

    Время передаётся явно (`at`), потому что проверять «в 9:00 придёт сводка»
    ожиданием девяти утра нельзя. Часы сервера при этом не трогаются: сдвигать
    их пришлось бы всем сразу, а сюда время приходит одним параметром.
    """
    from app import scheduler

    raw = payload.get("at")
    now = datetime.fromisoformat(raw) if raw else local_now()
    since = journal.cursor()
    with journal.capture() as warnings:
        scheduler.tick(now)
    return {"at": now.isoformat(), "messages": journal.messages(since=since),
            "warnings": warnings, "cursor": journal.cursor()}


# --- вспомогательное ------------------------------------------------------

def _pending(db: Session, user: User) -> List[dict]:
    import json as _json

    rows = (db.query(PendingAction)
            .filter(PendingAction.user_id == user.id, PendingAction.status == "pending")
            .order_by(PendingAction.id).all())
    result = []
    for row in rows:
        try:
            arguments = _json.loads(row.arguments_json or "{}")
        except ValueError:
            arguments = {"не разобрано": row.arguments_json}
        result.append({"id": row.id, "tool": row.tool, "arguments": arguments,
                       "status": row.status})
    return result


def _last_run_id(db: Session, user: User) -> int:
    row = (db.query(AgentRun.id).filter(AgentRun.user_id == user.id)
           .order_by(AgentRun.id.desc()).first())
    return row.id if row else 0


def _run_payload(db: Session, user: User, after_id: int) -> Optional[dict]:
    """Трейс прогона, который только что случился, — если запись включена."""
    row = (db.query(AgentRun)
           .filter(AgentRun.user_id == user.id, AgentRun.id > after_id)
           .order_by(AgentRun.id.desc()).first())
    return tracing.run_payload(db, row) if row is not None else None
