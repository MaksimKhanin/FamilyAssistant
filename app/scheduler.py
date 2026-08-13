"""Расписания и фоновая уборка.

    python -m app.scheduler

Runs beside the web app and the bot. It does three things, once a minute:

  * fires the family's scheduled jobs (утренняя сводка, вечерний итог, разбор недели);
  * delivers reminders whose time has come («напомни в пятницу утром»);
  * rotates camera media past its retention window.

It never talks to a channel directly — it publishes on the Event Bus, and whoever
owns the channel (today: web push from the panel) delivers it.
"""
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.clock import local_now, to_local, to_utc, utc_now
from app.core.db import session_scope, upgrade_schema
from app.core.events import AGENT_MESSAGE, bus
from app.core.logging import get_logger
from app.core.models import ScheduledJob, User
from app.modules import load_modules

logger = get_logger("scheduler")

TICK_SEC = 60
RETENTION_HOUR = 4          # ротацию делаем ночью, когда дома всё равно тихо


def _due(job: ScheduledJob, now: datetime) -> bool:
    if not job.enabled:
        return False
    try:
        hour, minute = (int(part) for part in job.at_time.split(":"))
    except ValueError:
        logger.warning(f"Не разобрал время задачи {job.kind}: {job.at_time}")
        return False
    if (now.hour, now.minute) != (hour, minute):
        return False
    if job.weekday is not None and now.weekday() != job.weekday:
        return False
    # last_run_at, как и всё в базе, лежит в UTC — сравниваем в одной системе.
    if job.last_run_at and (now - to_local(job.last_run_at)) < timedelta(minutes=2):
        return False
    return True


def _digest_text(db: Session, user: User, kind: str) -> str:
    """Compose a job's message from the tools the person actually has switched on."""
    from app.agent.runtime import run_tool_directly
    from app.core.access import is_module_enabled
    from app.modules.memory import stats

    parts = []
    if kind in ("morning_digest", "weekly_review") and is_module_enabled(db, user.id, "security"):
        result = run_tool_directly(db, user, "get_security_log",
                                   {"period": "today" if kind == "morning_digest" else "week"},
                                   mode="schedule")
        if result.ok:
            parts.append(result.summary)

    if is_module_enabled(db, user.id, "nutrition"):
        period = "week" if kind == "weekly_review" else "day"
        result = run_tool_directly(db, user, "get_nutrition_stats", {"period": period}, mode="schedule")
        if result.ok:
            parts.append(result.summary)

    # Регулярная статистика досок: своего расписания у задачи нет — она цепляется
    # к этой же сводке (тикет #31). Знания всегда включены, спрашивать не о чем.
    #
    # Своя защита от падения, потому что этот кусок — единственный в сводке, что
    # идёт не через `registry.execute` с его перехватом: сводка одного человека,
    # рухнув, унесла бы сводки всех, чья задача в этой минуте ещё не разослана,
    # — а `_due` требует точного попадания в минуту, и до завтра они не вернутся.
    # Здесь же и единственный поход к модели за формулировкой: он платный по
    # времени, поэтому задач у доски не больше пяти.
    try:
        parts.extend(stats.digest_parts(db, user, kind))
    except Exception:
        logger.exception(f"Статистика досок для {user.display_name} не собралась — "
                         f"остальная сводка уходит без неё")
        db.rollback()

    if not parts:
        return ""

    opening = {
        "morning_digest": "Доброе утро.",
        "evening_summary": "Вечерний итог.",
        "weekly_review": "Как прошла неделя.",
    }.get(kind, "")
    return f"{opening}\n\n" + "\n\n".join(parts)


def run_jobs(db: Session, now: datetime):
    for job in db.query(ScheduledJob).filter(ScheduledJob.enabled.is_(True)).all():
        if not _due(job, now):
            continue
        user = db.get(User, job.user_id)
        if user is None:
            continue

        text = _digest_text(db, user, job.kind)
        job.last_run_at = to_utc(now)
        db.commit()

        if not text:
            logger.info(f"Задача {job.kind} для {user.display_name}: рассказывать нечего")
            continue

        bus.publish(AGENT_MESSAGE, {"family_id": user.family_id, "user_ids": [user.id],
                                    "text": text, "severity": "info"})
        logger.info(f"Отправлена задача {job.kind} для {user.display_name}")


def run_reminders(db: Session, now: datetime):
    from app.modules.memory import reminders as reminders_service

    for reminder in reminders_service.due_reminders(db, now):
        user = db.get(User, reminder.user_id)
        if user is None:
            continue
        bus.publish(AGENT_MESSAGE, {"family_id": user.family_id, "user_ids": [user.id],
                                    "text": f"Напоминаю: {reminder.text}", "severity": "attention"})
        reminder.reminded_at = now
    db.commit()


def run_retention(db: Session):
    from app.modules.memory import reminders as reminders_service
    from app.modules.security.retention import rotate
    rotate(db)
    reminders_service.purge_fired(db)


def tick(now: datetime = None):
    # Расписания семьи — по местному времени, напоминания хранятся в UTC.
    local = now or local_now()
    with session_scope() as db:
        run_jobs(db, local)
        run_reminders(db, utc_now())
        if local.hour == RETENTION_HOUR and local.minute == 0:
            run_retention(db)


def prepare():
    """Загрузить модули и убедиться, что схема на месте.

    Планировщик и веб поднимаются одновременно, и рассчитывать на то, что схему
    довёл до головы кто-то другой, нельзя: проиграв гонку, планировщик раз в минуту
    спрашивал бы несуществующую таблицу и засыпал бы лог базы ошибками.
    `upgrade_schema` идемпотентен и берёт блокировку — кто пришёл первым, тот и накатил.
    """
    load_modules()
    upgrade_schema()


def main():
    prepare()
    bus.start()
    logger.info("Планировщик запущен")

    while True:
        started = time.monotonic()
        try:
            tick()
        except Exception:
            logger.exception("Ошибка в цикле планировщика — продолжаю")
        time.sleep(max(1.0, TICK_SEC - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
