"""Экраны «Знания» и «Напоминания».

Экран знаний исключён из режима «от лица» целиком (ADR-0005): содержимое всегда
принадлежит тому, кто смотрит, — `current`, а не `viewed`, — и переключение
аватара в шапке его не меняет. `viewed` здесь нужен только каркасу (ряд аватаров
в шапке), к содержимому он не прикасается; `can_act_as` не проверяется — править
своё можно всегда.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render
from app.modules.memory import knowledge, reminders, service
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
    section: str = "",
    kind: str = "",
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    context = screen_context(
        request, db, current, viewed,
        title="Знания",
        subtitle="Ваши разделы — и то, что ассистент держит в голове",
    )
    active_section = None
    # isdecimal, а не isdigit: isdigit пропускает «²», на котором падает int();
    # потолок отсекает числа, не влезающие в INTEGER на боевой базе.
    if section.isdecimal() and len(section) <= 9:
        active_section = knowledge.get_section(db, current.id, int(section))
    common_active = section == "common"
    context.update(
        sections=knowledge.list_sections(db, current.id),
        active_section=active_section,
        common_active=common_active,
    )
    if active_section is None and not common_active:
        context.update(
            notes=service.list_notes(db, current.id, kind=kind or None),
            counters=service.counters(db, current.id),
            filters=FILTERS,
            active_filter=kind,
            kind_labels=KIND_LABELS,
        )
    return render(request, "memory/memory.html", context)


# --- разделы (тикет #25) ----------------------------------------------------

@router.post("/sections/add")
def add_section(
    name: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    section = knowledge.create_section(db, current.id, name)
    target = f"/memory?section={section.id}" if section else "/memory"
    return RedirectResponse(target, status_code=303)


@router.post("/sections/{section_id}/rename")
def rename_section(
    section_id: int,
    name: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.rename_section(db, current.id, section_id, name)
    return RedirectResponse(f"/memory?section={section_id}", status_code=303)


@router.post("/sections/{section_id}/pin")
def pin_section(
    section_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.toggle_pin(db, current.id, section_id)
    return RedirectResponse(f"/memory?section={section_id}", status_code=303)


@router.post("/sections/{section_id}/delete")
def delete_section(
    section_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.delete_section(db, current.id, section_id)
    return RedirectResponse("/memory", status_code=303)


# --- заметки: живут до переезда на доски (#33) --------------------------------

@router.post("/add")
def add_note(
    text: str = Form(...),
    kind: str = Form("fact"),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    if text.strip():
        service.add_note(db, current.id, text=text, kind=kind, source="добавлено вручную")
    return RedirectResponse("/memory", status_code=303)


@router.post("/{note_id}/pin")
def pin_note(
    note_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    service.toggle_pin(db, current.id, note_id)
    return RedirectResponse("/memory", status_code=303)


@router.post("/{note_id}/forget")
def forget_note(
    note_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    service.forget(db, current.id, note_id)
    return RedirectResponse("/memory", status_code=303)
