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


def _decimal_id(raw: str):
    """isdecimal, а не isdigit: isdigit пропускает «²», на котором падает int();
    потолок отсекает числа, не влезающие в INTEGER на боевой базе."""
    return int(raw) if raw.isdecimal() and len(raw) <= 9 else None


@router.get("", response_class=HTMLResponse)
def memory_screen(
    request: Request,
    section: str = "",
    board: str = "",
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
    section_id = _decimal_id(section)
    active_section = None if section_id is None else knowledge.get_section(db, current.id, section_id)
    common_active = section == "common"
    context.update(
        sections=knowledge.list_sections(db, current.id),
        active_section=active_section,
        common_active=common_active,
    )
    if active_section is not None:
        boards = knowledge.list_boards(db, current.id, active_section.id)
        board_id = _decimal_id(board)
        active_board = None if board_id is None else next(
            (b for b in boards if b.id == board_id), None)
        context.update(boards=boards, active_board=active_board)
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


# --- доски (тикет #26) --------------------------------------------------------

@router.post("/boards/add")
def add_board(
    section_id: int = Form(...),
    name: str = Form(...),
    instruction: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    board = knowledge.create_board(db, current.id, section_id, name, instruction)
    target = (f"/memory?section={section_id}&board={board.id}" if board
              else f"/memory?section={section_id}")
    return RedirectResponse(target, status_code=303)


@router.post("/boards/{board_id}/update")
def update_board(
    board_id: int,
    name: str = Form(...),
    instruction: str = Form(""),
    section_id: int = Form(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    board = knowledge.update_board(db, current.id, board_id, name, instruction,
                                   section_id=section_id)
    target = (f"/memory?section={board.section_id}&board={board.id}" if board
              else "/memory")
    return RedirectResponse(target, status_code=303)


@router.post("/boards/{board_id}/delete")
def delete_board(
    board_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    board = knowledge.get_board(db, current.id, board_id)
    section_id = board.section_id if board else None
    knowledge.delete_board(db, current.id, board_id)
    target = f"/memory?section={section_id}" if section_id else "/memory"
    return RedirectResponse(target, status_code=303)


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
