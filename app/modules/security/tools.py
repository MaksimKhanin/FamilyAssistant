"""Agent tools for the security module.

`classify_event` is the expensive second opinion, invoked only for events the cheap
rules were unsure about — and it honours the family's «Кадры с камер не покидают
дом» switch: with it on, the model is given a textual description of the detection
instead of the frame itself.
"""
from datetime import datetime
from pathlib import Path

from app.agent.llm import LLMUnavailable, client as llm_client, image_part, text_part
from app.agent.prompts import EVENT_CLASSIFY_SYSTEM
from app.agent.registry import ToolContext, ToolResult, tool
from app.core import family as family_service
from app.core.events import AGENT_MESSAGE, bus
from app.core.logging import get_logger
from app.core.templating import counted, filesize
from app.modules.security import retention, service
from app.modules.security.models import VERDICT_ANOMALY, VERDICT_CHECK, VERDICT_LABELS, VERDICT_NORMAL, Camera

MODULE = "security"
logger = get_logger("security.tools")

SEVERITIES = ("info", "attention", "alarm")


def _days(count: int) -> str:
    """«старше 1 дня», «старше 2 дней» — родительный падеж после «старше»."""
    return counted(count, "дня", "дней", "дней")


@tool(
    name="get_security_log",
    module=MODULE,
    title="Показать события дома",
    description="""
    Что происходило дома за период. period: today | week | month.
    only: all — всё, anomaly — только то, на что стоит взглянуть, normal — штатное.
    """,
    parameters={
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["today", "week", "month"]},
            "only": {"type": "string", "enum": ["all", "anomaly", "normal"]},
        },
    },
    read_only=True,
)
def get_security_log(ctx: ToolContext, period: str = "today", only: str = "all") -> ToolResult:
    days = {"today": 1, "week": 7, "month": 30}.get(period, 1)
    events = service.list_events(ctx.db, ctx.family_id, only=only, days=days)
    if not events:
        return ToolResult(summary="За этот период дома было тихо.", data={"events": []})

    cameras = {c.id: c for c in service.list_cameras(ctx.db, ctx.family_id)}
    notable = [e for e in events if e.is_anomaly]
    lines = [f"- {service.describe(e, cameras.get(e.camera_id))} ({e.verdict_label})" for e in events[:8]]

    summary = (f"Событий: {len(events)}, из них требуют взгляда — {len(notable)}.\n" + "\n".join(lines))
    card = None
    if notable:
        latest = notable[0]
        camera = cameras.get(latest.camera_id)
        card = {
            "type": "security",
            "event_id": latest.id,
            "title": service.describe(latest, camera),
            "camera": camera.label if camera else "",
            "verdict": latest.verdict,
            "at": latest.happened_at.strftime("%H:%M"),
        }
    return ToolResult(summary=summary, data={"total": len(events), "anomalies": len(notable)}, card=card)


@tool(
    name="mark_events_seen",
    module=MODULE,
    title="Отметить события просмотренными",
    description="""
    Погасить значок «непросмотренное» на событиях дома: «я всё видел», «убери
    уведомления», «пометь просмотренным всё, что старше двух дней». Записи
    остаются в ленте — снимается только пометка «никто не разобрал».
    older_than_days: 0 — всё сразу, N — только то, что старше N суток.
    """,
    parameters={
        "type": "object",
        "properties": {
            "older_than_days": {
                "type": "integer", "minimum": 0,
                "description": "Возраст в сутках; 0 — пометить всё",
            },
        },
    },
    # Ничего не пропадает: событие остаётся в ленте вместе со своим вердиктом.
    auto_from=2,
)
def mark_events_seen(ctx: ToolContext, older_than_days: int = 0) -> ToolResult:
    older_than_days = max(0, older_than_days)
    changed = service.mark_seen(ctx.db, ctx.family_id, older_than_days=older_than_days)
    left = service.unseen_count(ctx.db, ctx.family_id)

    if not changed:
        return ToolResult(summary="Непросмотренных событий и так не было — значок не горит.",
                          data={"marked": 0, "unseen_left": left})

    period = "" if not older_than_days else f", что старше {_days(older_than_days)}"
    tail = f" Осталось непросмотренных: {left}." if left else " Значок погас."
    return ToolResult(
        summary=f"Отметил просмотренным всё{period}: "
                f"{counted(changed, 'событие', 'события', 'событий')}. "
                f"В ленте они остались.{tail}",
        data={"marked": changed, "unseen_left": left},
    )


@tool(
    name="clear_archive",
    module=MODULE,
    title="Убрать старые записи с камер",
    description="""
    Удалить записи архива старше указанного срока: «почисти архив за месяц»,
    «удали всё, что старше недели», «убери старые записи с калитки».
    Файлы стираются с диска насовсем; события в ленте остаются, только без кадров.
    older_than_days — сколько суток оставить нетронутыми, минимум 1.
    camera — название камеры, если человек назвал одну; без него чистится весь архив.
    """,
    parameters={
        "type": "object",
        "properties": {
            "older_than_days": {"type": "integer", "minimum": 1,
                                "description": "Убрать всё, что старше стольких суток"},
            "camera": {"type": "string", "description": "Название камеры, если названа одна"},
        },
        "required": ["older_than_days"],
    },
    # Как и удаление еды: файлов потом не вернуть, поэтому спрашиваем на всех
    # уровнях, кроме максимального.
    auto_from=3,
)
def clear_archive(ctx: ToolContext, older_than_days: int, camera: str = None) -> ToolResult:
    if older_than_days < 1:
        return ToolResult(summary="Так я сотру и сегодняшние записи. Скажите срок от суток и больше.",
                          ok=False)

    selected = None
    if camera:
        selected = _find_camera(ctx, camera)
        if selected is None:
            known = ", ".join(f"«{c.label}»" for c in service.list_cameras(ctx.db, ctx.family_id))
            return ToolResult(summary=f"Не нашёл камеру «{camera}». Есть: {known or 'ни одной'}.",
                              ok=False)

    result = retention.purge(ctx.db, ctx.family_id, older_than_days,
                             camera_id=selected.id if selected else None)
    where = f" у камеры «{selected.label}»" if selected else ""
    if not result["records"]:
        return ToolResult(summary=f"Записей старше {_days(older_than_days)}{where} в архиве нет — "
                                  f"убирать было нечего.",
                          data=result)

    freed = filesize(result["bytes"])
    return ToolResult(
        summary=f"Убрал из архива{where} {counted(result['records'], 'запись', 'записи', 'записей')} "
                f"старше {_days(older_than_days)}"
                f"{f', освободилось {freed}' if freed else ''}. "
                f"События в ленте остались, только без кадров.",
        data=result,
    )


def _find_camera(ctx: ToolContext, name: str) -> Camera:
    """Камера по тому, как её назвал человек: «калитка», «Калитка», слаг из ingest."""
    wanted = name.strip().lower()
    cameras = service.list_cameras(ctx.db, ctx.family_id)
    return next((c for c in cameras if wanted in (c.label.lower(), c.slug.lower())), None)


@tool(
    name="classify_event",
    module=MODULE,
    title="Разобраться в событии",
    description="""
    Внимательно посмотреть на конкретное событие с камеры и решить, стоит ли оно
    внимания семьи. Вызывай, когда правила отметили событие как «проверить»,
    или когда человек спрашивает про конкретное событие.
    """,
    parameters={
        "type": "object",
        "properties": {"event_id": {"type": "integer"}},
        "required": ["event_id"],
    },
    auto_from=1,
)
def classify_event(ctx: ToolContext, event_id: int) -> ToolResult:
    event = service.get_event(ctx.db, ctx.family_id, event_id)
    if event is None:
        return ToolResult(summary="Такого события нет.", ok=False)

    camera = ctx.db.get(Camera, event.camera_id)
    settings_row = family_service.get_settings(ctx.db, ctx.family_id)

    description = (
        f"Камера: «{camera.label if camera else '—'}», зона: {camera.zone if camera else '—'}.\n"
        f"Время: {event.happened_at:%d.%m %H:%M}.\n"
        f"Детектор увидел: {event.detected_class or 'движение'}"
        f"{f' (уверенность {event.confidence:.2f})' if event.confidence else ''}.\n"
        f"Предварительный вывод правил: {event.verdict_label} — {event.reason}."
    )

    content = [text_part(description)]
    frame = _load_frame(event.snapshot_path) if not settings_row.frames_stay_home else None
    if frame:
        content.append(image_part(frame))
    elif settings_row.frames_stay_home:
        content.append(text_part("Кадр не передаётся: семья попросила, чтобы снимки не покидали дом."))

    try:
        raw = llm_client.json_completion(EVENT_CLASSIFY_SYSTEM, content, max_tokens=400)
    except LLMUnavailable:
        return ToolResult(summary=f"Не смог разобрать событие — модель не отвечает. "
                                  f"Пока считаю так: {event.verdict_label}, {event.reason}.", ok=False)

    verdict = str(raw.get("verdict", "")).lower()
    if verdict not in VERDICT_LABELS:
        verdict = VERDICT_CHECK
    event.verdict = verdict
    event.reason = str(raw.get("reason") or event.reason)[:255]
    event.classified_by = "model"
    event.note = str(raw.get("message") or "")[:1000] or None
    ctx.db.commit()

    return ToolResult(
        summary=(f"Посмотрел событие {event.id}: {VERDICT_LABELS[verdict]}. "
                 f"{event.reason} Это оценка модели, а не факт."),
        data={"event_id": event.id, "verdict": verdict},
        card={
            "type": "security",
            "event_id": event.id,
            "title": service.describe(event, camera),
            "camera": camera.label if camera else "",
            "verdict": verdict,
            "at": event.happened_at.strftime("%H:%M"),
        },
    )


@tool(
    name="notify_family",
    module=MODULE,
    title="Сказать семье",
    description="""
    Отправить сообщение всем в семье (в Telegram). Используй только когда есть
    что-то действительно важное — тревога дома или срочное напоминание.
    severity: info — просто сообщить, attention — стоит взглянуть, alarm — тревога.
    Текст пиши спокойно, с оговоркой, если это оценка, а не факт.
    """,
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
        },
        "required": ["message"],
    },
    auto_from=3,
)
def notify_family(ctx: ToolContext, message: str, severity: str = "info") -> ToolResult:
    if severity not in SEVERITIES:
        severity = "info"

    recipients = [m.id for m in family_service.members(ctx.db, ctx.family_id)]
    bus.publish(AGENT_MESSAGE, {
        "family_id": ctx.family_id,
        "user_ids": recipients,
        "text": message,
        "severity": severity,
        "from_user_id": ctx.subject.id,
    })
    return ToolResult(summary=f"Отправил семье: {message}", data={"recipients": len(recipients)})


def _load_frame(path: str):
    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        logger.warning(f"Снимок события не найден: {path}")
        return None
    return file_path.read_bytes()


def auto_review(db, event_id: int, family_id: int):
    """Second opinion for events the rules were unsure about.

    Wired to the «камера что-то увидела» event so a `check` verdict does not just
    sit in the log waiting for someone to open the panel.
    """
    from app.agent.runtime import run_tool_directly

    event = service.get_event(db, family_id, event_id)
    if event is None or event.verdict != VERDICT_CHECK:
        return
    head = family_service.head_of_family(db, family_id)
    if head is None:
        return
    result = run_tool_directly(db, head, "classify_event", {"event_id": event_id}, mode="event")
    if not result.ok:
        logger.info(f"Событие {event_id} осталось с вердиктом правил: {result.summary}")


def notify_on_anomaly(payload: dict):
    """Turn a confirmed anomaly into one calm message to the family."""
    from app.core.db import session_scope

    with session_scope() as db:
        family_id = payload.get("family_id")
        event = service.get_event(db, family_id, payload.get("event_id"))
        if event is None or event.verdict != VERDICT_ANOMALY or event.notified_at:
            return
        camera = db.get(Camera, event.camera_id)
        if camera is not None and not camera.notify_enabled:
            logger.info(f"Камера «{camera.label}» настроена только на лог — не пишу семье")
            return

        text = event.note or (
            f"{service.describe(event, camera)} Похоже на постороннего — "
            f"но это оценка модели, а не факт."
        )
        bus.publish(AGENT_MESSAGE, {
            "family_id": family_id,
            "user_ids": [m.id for m in family_service.members(db, family_id)],
            "text": text,
            "severity": "alarm",
            "event_id": event.id,
        })
        event.notified_at = datetime.utcnow()


__all__ = ["get_security_log", "mark_events_seen", "clear_archive", "classify_event",
           "notify_family", "auto_review", "notify_on_anomaly", "VERDICT_ANOMALY", "VERDICT_NORMAL"]
