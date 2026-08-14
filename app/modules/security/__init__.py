"""Security module — камеры, фильтрация событий, уведомления о реальных аномалиях.

Family-shared data (scoped by `family_id`): everyone who has the module switched on
sees the same cameras and the same feed. The heavy lifting happens on the recorder
at home; this module receives what it found, decides what deserves the family's
attention, and says so once. Всё остальное — штатная запись — молча оседает в архиве.
"""
from app.core.events import SECURITY_ANOMALY
from app.core.module import Module, NavItem
from app.modules.security import models, tools  # noqa: F401  (регистрирует таблицы и инструменты)
from app.modules.security.ingest import router as ingest_router
from app.modules.security.routes import router as ui_router


def _on_anomaly(payload: dict):
    """Rules flagged something. Get a second opinion if unsure, then tell the family."""
    from app.core.db import session_scope

    if payload.get("verdict") == "check":
        with session_scope() as db:
            tools.auto_review(db, payload["event_id"], payload["family_id"])
    tools.notify_on_anomaly(payload)


module = Module(
    name="security",
    title="Безопасность",
    description="Смотрит за домом и пишет, только когда есть что сказать",
    routers=[ui_router, ingest_router],
    nav_items=[
        # В нижней панели пункт называется «Дом»: там их три на всю ширину, и
        # человек идёт туда не «за событиями», а посмотреть, что дома. В сайдборе
        # компьютера рядом стоят «Архив» и «Камеры», и там «События» точнее.
        NavItem(slug="events", label="События", short="Дом", url="/security/events",
                icon="shield", group="Безопасность", badge_key="anomaly_count"),
        NavItem(slug="archive", label="Архив", url="/security/archive", icon="archive",
                group="Безопасность"),
        NavItem(slug="cameras", label="Камеры", url="/security/cameras", icon="camera",
                group="Безопасность"),
    ],
    memo_hint=("Что учитывать, когда речь о доме: кто приходит и когда (уборщица по "
               "средам, курьеры днём), когда дома обычно никого, о чём писать сразу, "
               "а о чём не будить."),
    event_handlers={SECURITY_ANOMALY: [_on_anomaly]},
    per_user=False,
)
