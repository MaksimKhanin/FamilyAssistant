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
from app.core.clock import local_now, to_local
from app.core.db import get_db
from app.core.models import User
from app.core.templating import render, ru_date
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


#: Объяснения заблокированных действий — по коду из query-параметра, чтобы
#: редирект после POST оставался обычным путём панели.
NOTICES = {
    "board-shared": "Нельзя удалить: доской пользуются другие. Сначала отзовите доступ.",
    "section-shared": "Нельзя удалить раздел: в нём есть доска с активным доступом. "
                      "Сначала отзовите доступ.",
}


def _board_view(db: Session, current: User, grant, members) -> dict:
    """Всё, что нужно карточке доски: лента, право смотрящего, состав доступа."""
    entries = knowledge.list_entries(db, current.id, grant.board.id)
    return {
        "active_board": grant.board,
        "board_right": grant.right,
        "audience": knowledge.board_audience(db, current.id, grant.board.id),
        "right_labels": knowledge.RIGHT_LABELS,
        "feed": _feed_days(entries),
        # Величины, которые ассистент разобрал неуверенно: под их записями
        # висит тихая плашка уточнения (тикет #30).
        "clarify": knowledge.clarifications(db, grant.board.id, [e.id for e in entries]),
        "event_types": knowledge.list_event_types(db, grant.board.id),
        # Обрезанный хвост не выдаётся за целое: над лентой честная пометка.
        "feed_limit": knowledge.FEED_LIMIT if len(entries) >= knowledge.FEED_LIMIT else None,
        "author_names": {m.id: m.display_name for m in members},
    }


@router.get("", response_class=HTMLResponse)
def memory_screen(
    request: Request,
    section: str = "",
    board: str = "",
    kind: str = "",
    notice: str = "",
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
    board_id = _decimal_id(board)
    context.update(
        sections=knowledge.list_sections(db, current.id),
        active_section=active_section,
        common_active=common_active,
        notice_text=NOTICES.get(notice),
    )
    if active_section is not None:
        boards = knowledge.list_boards(db, current.id, active_section.id)
        active_board = None if board_id is None else next(
            (b for b in boards if b.id == board_id), None)
        context.update(boards=boards, active_board=active_board)
        if active_board is not None:
            grant = knowledge.board_access(db, current.id, active_board.id)
            context.update(_board_view(db, current, grant, context["members"]))
    elif common_active:
        shared = knowledge.shared_boards(db, current.id)
        active_grant = None if board_id is None else next(
            (g for g in shared if g.board.id == board_id), None)
        context.update(shared_grants=shared,
                       shared_owners=knowledge.board_owner_names(db, shared),
                       right_labels=knowledge.RIGHT_LABELS)
        if active_grant is not None:
            context.update(_board_view(db, current, active_grant, context["members"]))
    else:
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
    try:
        knowledge.delete_section(db, current.id, section_id)
    except knowledge.ActiveShares:
        return RedirectResponse(f"/memory?section={section_id}&notice=section-shared",
                                status_code=303)
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
    grant = knowledge.board_access(db, current.id, board_id)
    try:
        deleted = knowledge.delete_board(db, current.id, board_id)
    except knowledge.ActiveShares:
        return RedirectResponse(
            f"/memory?section={grant.board.section_id}&board={board_id}&notice=board-shared",
            status_code=303)
    if deleted:
        return RedirectResponse(f"/memory?section={grant.board.section_id}", status_code=303)
    # Не владелец (или доски нет): удаление не прошло — назад к доске её глазами.
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


# --- доступ (тикет #28) ---------------------------------------------------------

@router.post("/boards/{board_id}/share")
def share_board(
    board_id: int,
    member_id: int = Form(...),
    right: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.share_board(db, current.id, board_id, member_id, right)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


@router.post("/boards/{board_id}/share-all")
def share_board_with_all(
    board_id: int,
    right: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.share_board_with_all(db, current.id, board_id, right)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


@router.post("/boards/{board_id}/unshare")
def unshare_board(
    board_id: int,
    member_id: int = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.revoke_share(db, current.id, board_id, member_id)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


@router.post("/boards/{board_id}/unshare-all")
def unshare_board_with_all(
    board_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.stop_sharing_with_all(db, current.id, board_id)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


# --- записи (тикет #27) --------------------------------------------------------

def _feed_days(entries):
    """Лента с разделителями по дням: нужный день ищут глазами.

    Группировка по дате, а не по подписи: подпись без года («12 августа»)
    склеила бы соседние записи разных лет.
    """
    today = local_now().date()
    days = []
    for entry in entries:
        day = to_local(entry.created_at).date()
        if not days or days[-1]["day"] != day:
            label = ("Сегодня" if day == today
                     else "Вчера" if (today - day).days == 1
                     else ru_date(entry.created_at))
            days.append({"day": day, "label": label, "entries": []})
        days[-1]["entries"].append(entry)
    return days


def _board_url(db: Session, current: User, board_id: int) -> str:
    """Адрес доски глазами смотрящего: своя открывается в своём разделе,
    расшаренная — в «Общем»."""
    grant = knowledge.board_access(db, current.id, board_id) if board_id else None
    return knowledge.board_url(grant) if grant is not None else "/memory"


@router.post("/entries/add")
def add_entry(
    board_id: int = Form(...),
    text: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    knowledge.add_entry(db, current.id, board_id, text)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


@router.post("/entries/{entry_id}/edit")
def edit_entry(
    entry_id: int,
    text: str = Form(...),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    # Доска для возврата берётся до правки: отклонённая правка (пустой текст)
    # не должна выбрасывать человека с доски на корневой экран.
    entry = knowledge.get_entry(db, current.id, entry_id)
    board_id = entry.board_id if entry else None
    knowledge.edit_entry(db, current.id, entry_id, text)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


@router.post("/entries/{entry_id}/delete")
def delete_entry(
    entry_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    entry = knowledge.get_entry(db, current.id, entry_id)
    board_id = entry.board_id if entry else None
    knowledge.delete_entry(db, current.id, entry_id)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


# --- словарь величин доски (тикет #30) -----------------------------------------

@router.post("/boards/{board_id}/types/add")
def add_event_type(
    board_id: int,
    name: str = Form(...),
    unit: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Завести тип величины: с него на доске начинается разбор записей."""
    knowledge.add_event_type(db, current.id, board_id, name, unit)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


# --- уточнение разобранной величины (тикет #30) --------------------------------

@router.post("/events/{event_id}/clarify")
def clarify_event(
    event_id: int,
    kind: str = Form(""),
    own: str = Form(""),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    """Ответ на плашку: нажатый вариант или свои слова из поля рядом.

    Свои слова важнее нажатой кнопки: человек дописал их, уже видя варианты.
    """
    # Доска берётся до ответа: отклонённое уточнение (пустые слова) не должно
    # выбрасывать человека с доски на корневой экран.
    board_id = knowledge.event_board(db, current.id, event_id)
    knowledge.clarify_event(db, current.id, event_id, own.strip() or kind)
    return RedirectResponse(_board_url(db, current, board_id), status_code=303)


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
