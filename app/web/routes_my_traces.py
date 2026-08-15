"""«Мои трейсы» — участнический экран: свои разговоры с ассистентом для отладки.

Пара к админскому `/settings/traces` (там — вся семья и полные промпты, экран
закрыт для участников). Здесь — то же самое, но только свои прогоны: участник
видит, что на самом деле ушло в модель и что вернулось, когда хочет понять,
почему ассистент ответил именно так. Роль тут не проверяется явно: адрес по
умолчанию участниковый (`app/core/roles.py`), а у админской учётки нет ни
разговора с ассистентом, ни своих прогонов, поэтому попадать сюда ей незачем.

Выборка везде идёт от `current`, а не от `viewed`: переключение аватара в шапке —
витрина для чужих экранов (`get_viewed_user`), а не пропуск к чужой переписке.
"""
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.agent import tracing
from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.web.context import screen_context

router = APIRouter(prefix="/settings/my-traces", tags=["my-traces"])

RUNS_ON_SCREEN = 40


@router.get("", response_class=HTMLResponse)
def my_traces_screen(
    request: Request,
    session_id: str = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Мои трейсы",
                             subtitle="Свои разговоры с ассистентом — для отладки")
    rows = tracing.runs(db, limit=RUNS_ON_SCREEN, user_id=current.id, session_id=session_id)
    context.update(
        trace_enabled=tracing.enabled(db, current.family_id),
        by_session=tracing.by_session(db, current.family_id, user_id=current.id),
        runs=[tracing.run_view(db, row) for row in rows],
        filter_session_id=session_id,
        export_query=(f"?session_id={session_id}" if session_id else ""),
    )
    return render(request, "settings/my_traces.html", context)


@router.get("/export.json")
def export(
    session_id: str = None,
    run_id: int = None,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    data = tracing.own_export(db, current, session_id=session_id, run_id=run_id)
    name = "my-traces" + (f"-session-{session_id}" if session_id else "") + \
           (f"-run-{run_id}" if run_id else "")
    return Response(
        content=json.dumps(data, ensure_ascii=False, indent=2, default=str),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
    )
