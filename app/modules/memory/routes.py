"""Web screen «Память и заметки»."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import can_act_as, get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.modules.memory import reminders, service
from app.modules.memory.models import KIND_LABELS
from app.web.context import screen_context

router = APIRouter(prefix="/memory", tags=["memory"])

reminders_router = APIRouter(prefix="/reminders", tags=["reminders"])


@reminders_router.get("", response_class=HTMLResponse)
def reminders_screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(
        request, db, current, viewed,
        title="Напоминания",
        subtitle="О чём ассистент напомнит — и о чём уже напомнил",
    )
    context.update(
        active=reminders.list_active(db, viewed.id),
        fired=reminders.list_fired(db, viewed.id),
        fired_retention_days=reminders.FIRED_RETENTION_DAYS,
    )
    return render(request, "memory/reminders.html", context)

FILTERS = [("", "Всё"), ("task", "Напоминания"), ("pref", "Предпочтения"),
           ("health", "Здоровье"), ("fact", "Наблюдения")]


@router.get("", response_class=HTMLResponse)
def memory_screen(
    request: Request,
    kind: str = "",
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(
        request, db, current, viewed,
        title="Память и заметки",
        subtitle="То, что ассистент держит в голове про вас и семью",
    )
    context.update(
        notes=service.list_notes(db, viewed.id, kind=kind or None),
        counters=service.counters(db, viewed.id),
        filters=FILTERS,
        active_filter=kind,
        kind_labels=KIND_LABELS,
        editable=can_act_as(current, viewed),
    )
    return render(request, "memory/memory.html", context)


@router.post("/add")
def add_note(
    text: str = Form(...),
    kind: str = Form("fact"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if text.strip() and can_act_as(current, viewed):
        service.add_note(db, viewed.id, text=text, kind=kind, source="добавлено вручную")
    return RedirectResponse("/memory", status_code=303)


@router.post("/{note_id}/pin")
def pin_note(
    note_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed):
        service.toggle_pin(db, viewed.id, note_id)
    return RedirectResponse("/memory", status_code=303)


@router.post("/{note_id}/forget")
def forget_note(
    note_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    if can_act_as(current, viewed):
        service.forget(db, viewed.id, note_id)
    return RedirectResponse("/memory", status_code=303)
