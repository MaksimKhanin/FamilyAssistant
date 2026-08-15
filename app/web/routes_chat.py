"""Разговор с ассистентом — главный экран панели и выдвижная панель на компьютере.

The same conversation as in Telegram: both channels write to `chat_messages` and
both go through `Agent.respond`. The panel is HTMX — a message posts a form and
gets rendered message bubbles back, including the tool traces and action cards.

Экран (`/chat`) и панель (`/chat/panel`) — одна и та же переписка в двух рамах.
На телефоне разговор занимает весь экран: именно ради него приложение и
открывают, а оверлей поверх чего-то каждый раз требовал сначала попасть на это
«что-то». На компьютере сбоку есть место, и панель остаётся.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.agent.runtime import AgentReply, agent, approve_action, message_payload, reject_action
from app.core.auth import get_current_user, get_viewed_user
from app.core.clock import day_bounds_utc, local_now, local_today, to_local
from app.core.db import get_db
from app.core.models import ChatMessage, User
from app.core.templating import render, ru_date
from app.web.context import screen_context

router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_ON_OPEN = 12

# --- очистка переписки ------------------------------------------------------

#: Тот же набор окон, что у чистки статистики питания: «сегодня», «последние
#: семь дней», «последние 30 дней» — и отдельно «всё» без окна вообще.
PERIODS = {"day": 1, "week": 7, "month": 30}
PERIOD_LABELS = {"all": "Всё", "day": "Сегодня", "week": "Неделя", "month": "Месяц"}
PERIOD_WINDOWS = {"all": "всю переписку", "day": "переписку за сегодня",
                  "week": "переписку за последние семь дней",
                  "month": "переписку за последние 30 дней"}


def clear_history(db: Session, user_id: int, period: str = "all") -> int:
    """Стереть переписку — всю или за скользящее окно до сегодня включительно."""
    query = db.query(ChatMessage).filter(ChatMessage.user_id == user_id)
    if period in PERIODS:
        start, _ = day_bounds_utc(local_today() - timedelta(days=PERIODS[period] - 1))
        query = query.filter(ChatMessage.created_at >= start)
    removed = query.delete(synchronize_session=False)
    db.commit()
    return removed

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
    messages = [{"role": m.role, "text": m.content, "at": m.created_at, **message_payload(m)}
                for m in reversed(rows)]
    return _with_day_separators(messages)


def _with_day_separators(messages: list) -> list:
    """Разделитель дня над первым сообщением каждого дня.

    Разговор с ассистентом длится месяцами, и без дат вчерашний ответ читается
    как сегодняшний. Метка ставится на историю целиком — у ответа, который
    прилетает по HTMX в конец ленты, дня нет: он всегда сегодняшний, и
    отдельная надпись «Сегодня» посреди переписки только сбивала бы.
    """
    today = local_now().date()
    previous = None
    for message in messages:
        day = to_local(message["at"]).date() if message.get("at") else None
        if day and day != previous:
            message["daysep"] = ("Сегодня" if day == today
                                 else "Вчера" if (today - day).days == 1
                                 else ru_date(message["at"]))
            previous = day
    return messages


@router.get("", response_class=HTMLResponse)
def screen(
    request: Request,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
    viewed: User = Depends(get_viewed_user),
):
    """Разговор как экран: шапка с профилем, лента, ввод."""
    context = screen_context(request, db, current, viewed,
                             title="Разговор", subtitle="Скажите словами — остальное подберёт ассистент")
    context.update(
        messages=_history(db, current),
        suggestions=SUGGESTIONS,
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
        # Ассистент отвечает тому, кто спрашивает: действовать за другого больше
        # некому — роль главы семьи разделилась на админа и участника (ADR-0008).
        subject=current,
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
