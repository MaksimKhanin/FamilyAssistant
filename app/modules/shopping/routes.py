"""Экран «Покупки» — общий чеклист семьи.

Экран участниковый по умолчанию (app/core/roles.py): адрес не админский и не
общий, поэтому его видит тот, кому включён модуль. Список один на семью —
смотрят и правят его все одинаково, «глазами другого» тут смотреть не на что.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.modules.shopping import service
from app.web.context import screen_context

router = APIRouter(prefix="/shopping", tags=["shopping"])


@router.get("", response_class=HTMLResponse)
def screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(request, db, current, viewed,
                             title="Покупки",
                             subtitle="Один список на семью: положить, купить, вычеркнуть")
    context.update(
        items=service.list_items(db, current.family_id),
        item_limit=service.ITEM_LIMIT,
    )
    return render(request, "shopping/list.html", context)


@router.post("/add")
def add(
    text: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    texts = [part for part in (piece.strip() for piece in text.split(",")) if part]
    if texts:
        service.add_items(db, current.family_id, current.id, texts)
    return RedirectResponse("/shopping", status_code=303)


@router.post("/{item_id}/toggle")
def toggle(
    item_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    service.toggle(db, current.family_id, item_id)
    return RedirectResponse("/shopping", status_code=303)


@router.post("/{item_id}/delete")
def delete(
    item_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    service.delete_item(db, current.family_id, item_id)
    return RedirectResponse("/shopping", status_code=303)
