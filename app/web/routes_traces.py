"""«Трейсы агента» — админский экран: что ушло в модель, что вернулось, во что обошлось.

Раздел только для главы семьи: в трейсах лежат полные промпты, а значит и куски
переписки всех домашних. Здесь же — выключатель записи, очистка и выгрузка в JSON.
"""
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.agent import tracing
from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.web.context import screen_context

router = APIRouter(prefix="/settings/traces", tags=["traces"])

RUNS_ON_SCREEN = 40


def _back(user_id: int = None, session_id: str = None) -> RedirectResponse:
    query = []
    if user_id:
        query.append(f"user_id={user_id}")
    if session_id:
        query.append(f"session_id={session_id}")
    suffix = ("?" + "&".join(query)) if query else ""
    return RedirectResponse(f"/settings/traces{suffix}", status_code=303)


@router.get("", response_class=HTMLResponse)
def traces_screen(
    request: Request,
    user_id: int = None,
    session_id: str = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Трейсы агента",
                             subtitle="Промпты, вызовы инструментов и токены")
    if not current.is_head:
        return render(request, "settings/traces_denied.html", context)

    rows = tracing.runs(db, limit=RUNS_ON_SCREEN, user_id=user_id, session_id=session_id)
    context.update(
        trace_settings=tracing.get_settings(db, viewed.family_id),
        by_user=tracing.by_user(db, viewed.family_id),
        by_session=tracing.by_session(db, viewed.family_id),
        runs=[tracing.run_view(db, row) for row in rows],
        filter_user_id=user_id,
        filter_session_id=session_id,
        export_query=_export_query(user_id, session_id),
    )
    return render(request, "settings/traces.html", context)


def _export_query(user_id: int = None, session_id: str = None, run_id: int = None) -> str:
    parts = [f"{name}={value}" for name, value in
             (("user_id", user_id), ("session_id", session_id), ("run_id", run_id)) if value]
    return ("?" + "&".join(parts)) if parts else ""


@router.get("/export.json")
def export(
    user_id: int = None,
    session_id: str = None,
    run_id: int = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if not current.is_head:
        return RedirectResponse("/settings/traces", status_code=303)

    data = tracing.export(db, viewed.family_id, user_id=user_id,
                          session_id=session_id, run_id=run_id)
    name = "traces" + (f"-session-{session_id}" if session_id else "") + \
           (f"-run-{run_id}" if run_id else "") + (f"-user-{user_id}" if user_id else "")
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
    )


@router.post("/toggle")
def toggle(
    enabled: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if current.is_head:
        row = tracing.get_settings(db, viewed.family_id)
        row.enabled = enabled == "on"
        db.commit()
    return _back()


@router.post("/keep")
def set_keep(
    keep_runs: int = Form(300),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if current.is_head:
        row = tracing.get_settings(db, viewed.family_id)
        row.keep_runs = max(10, min(5000, keep_runs))
        db.commit()
    return _back()


@router.post("/clear")
def clear(
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if current.is_head:
        tracing.clear(db, viewed.family_id)
    return _back()
