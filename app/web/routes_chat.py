"""Разговор с ассистентом — главный экран панели и выдвижная панель на компьютере.

The same conversation as in Telegram: both channels write to `chat_messages` and
both go through `Agent.respond`. The panel is HTMX — a message posts a form and
gets rendered message bubbles back, including the tool traces and action cards.

Экран (`/chat`) и панель (`/chat/panel`) — одна и та же переписка в двух рамах.
На телефоне разговор занимает весь экран: именно ради него приложение и
открывают, а оверлей поверх чего-то каждый раз требовал сначала попасть на это
«что-то». На компьютере сбоку есть место, и панель остаётся.
"""
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.agent.runtime import AgentReply, agent, approve_action, message_payload, reject_action
from app.core.auth import get_current_user, get_viewed_user
from app.core.db import get_db
from app.core.models import ChatMessage, User
from app.core.templating import render
from app.web import day as day_service
from app.web.context import screen_context

router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_ON_OPEN = 12

SUGGESTIONS = [
    "Съел суп и салат",
    "Что ты помнишь?",
    "Что было ночью дома?",
    "Предложи ужин",
]


def _history(db: Session, user: User):
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(HISTORY_ON_OPEN)
        .all()
    )
    return [{"role": m.role, "text": m.content, "at": m.created_at, **message_payload(m)}
            for m in reversed(rows)]


@router.get("", response_class=HTMLResponse)
def screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Разговор как экран: шапка с профилем и цифрами дня, лента, ввод."""
    context = screen_context(request, db, current, viewed,
                             title="Разговор", subtitle="Скажите словами — остальное подберёт ассистент")
    context.update(
        messages=_history(db, current),
        suggestions=SUGGESTIONS,
        tiles=day_service.day_header(db, current, viewed, context["enabled_modules"]),
    )
    return render(request, "chat.html", context)


@router.get("/panel", response_class=HTMLResponse)
def panel(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    return render(request, "partials/chat_panel.html", {
        "request": request,
        "messages": _history(db, current),
        "suggestions": SUGGESTIONS,
    })


def _render(request: Request, entries: list) -> HTMLResponse:
    return render(request, "partials/chat_messages.html",
                                      {"request": request, "messages": entries})


@router.post("/send", response_class=HTMLResponse)
async def send(
    request: Request,
    message: str = Form(""),
    photo: UploadFile = File(None),
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    image = await photo.read() if photo is not None and photo.filename else None
    text = message.strip()
    if not text and not image:
        return _render(request, [])

    reply: AgentReply = agent.respond(
        db, current, text, image=image, channel="web",
        # действовать «за» другого участника может только глава семьи
        subject=viewed if current.is_head or viewed.id == current.id else current,
    )

    # Пузырь с репликой человека рисует сам браузер — сразу, не дожидаясь модели
    # (см. htmx:beforeRequest в base.html). Здесь — только ответ ассистента.
    return _render(request, [{"role": "assistant", "text": reply.text, **reply.to_payload()}])


@router.post("/actions/{pending_id}/approve", response_class=HTMLResponse)
def approve(
    pending_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    result = approve_action(db, pending_id, current, channel="web")
    return _render(request, [{
        "role": "assistant",
        "text": result.summary,
        "traces": [],
        "cards": [result.card] if result.card else [],
    }])


@router.post("/actions/{pending_id}/reject", response_class=HTMLResponse)
def reject(
    pending_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    result = reject_action(db, pending_id, current)
    return _render(request, [{"role": "assistant", "text": result.summary, "traces": [], "cards": []}])
