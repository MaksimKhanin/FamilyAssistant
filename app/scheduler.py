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

from app.agent import voice
from app.core.clock import local_now, to_local, to_utc, utc_now
from app.core.db import create_all, session_scope
from app.core.events import AGENT_MESSAGE, bus
from app.core.logging import get_logger
from app.core.models import ScheduledJob, User
from app.modules import load_modules

logger = get_logger("scheduler")

TICK_SEC = 60
RETENTION_HOUR = 4          # ротацию делаем ночью, когда дома всё равно тихо

#: Не больше стольких разборов «Подхода» за один тик — см. run_relationship_reviews.
REVIEW_BATCH_PER_TICK = 3

#: Чем открывается сводка, когда голос персоны недоступен, — прежние строки.
OPENINGS = {
    "morning_digest": "Доброе утро.",
    "evening_summary": "Вечерний итог.",
    "weekly_review": "Как прошла неделя.",
}

#: Какой момент дня у сводки — для просьбы к голосу персоны.
_MOMENTS = {
    "morning_digest": "утро",
    "evening_summary": "вечер",
    "weekly_review": "конец недели",
}

#: Просьбы к голосу персоны (`app/agent/voice.py`). Факты собраны кодом и
#: инструментами — работа модели здесь только слова, как у BOARD_STATS_SYSTEM.
_DIGEST_HINT = (
    "Сейчас {moment}, {date}. Перескажи человеку его сводку в своей манере — "
    "коротко, строго по фактам ниже: ничего не добавляй, числа не пересчитывай "
    "и не опускай. Это сообщение придёт ему уведомлением.\n\nФакты:\n{facts}"
)
_REMINDER_HINT = (
    "Пришло время напоминания, которое человек сам себе поставил. Напомни ему "
    "одной фразой в своей манере, обязательно назвав само дело его словами: "
    "«{text}». Ничего не добавляй и не выдумывай."
)


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


def _digest_parts(db: Session, user: User, kind: str, now: datetime) -> list:
    """Compose a job's facts from the tools the person actually has switched on.

    `now` — тот же момент, для которого `run_jobs` уже решил, что задаче пора
    сработать. Статистика досок считает по нему свои сутки/неделю (`window` в
    `app/modules/memory/stats.py`); без явной передачи `digest_parts` брала бы
    `utc_now()` заново — второй, отдельный от прогона момент времени, из-за
    которого цифра могла молча не попасть в собственную сводку.
    """
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
    # рухнув, унесла бы сводки всех, чья задача в этой минуте ещё не разослана.
    # Здесь же и поход к модели за формулировкой цифры: он платный по времени,
    # поэтому задач у доски не больше пяти.
    try:
        parts.extend(stats.digest_parts(db, user, kind, now=now))
    except Exception:
        logger.exception(f"Статистика досок для {user.display_name} не собралась — "
                         f"остальная сводка уходит без неё")
        db.rollback()

    return parts


def _digest_text(db: Session, user: User, kind: str, now: datetime) -> str:
    """Сводка целиком, прежним каноническим форматом. Оставлена как запасной
    текст для голоса персоны и как контракт для тестов и стенда."""
    parts = _digest_parts(db, user, kind, now)
    if not parts:
        return ""
    return f"{OPENINGS.get(kind, '')}\n\n" + "\n\n".join(parts)


def run_jobs(db: Session, now: datetime):
    for job in db.query(ScheduledJob).filter(ScheduledJob.enabled.is_(True)).all():
        if not _due(job, now):
            continue
        user = db.get(User, job.user_id)
        if user is None:
            continue

        parts = _digest_parts(db, user, job.kind, to_utc(now))
        job.last_run_at = to_utc(now)
        db.commit()

        if not parts:
            logger.info(f"Задача {job.kind} для {user.display_name}: рассказывать нечего")
            continue

        # Факты собраны; произносит их голос персоны, а при недоступной модели —
        # прежняя каноническая сводка. Числа модель не считает — только слова.
        fallback = f"{OPENINGS.get(job.kind, '')}\n\n" + "\n\n".join(parts)
        text = voice.speak(user, _DIGEST_HINT.format(
            moment=_MOMENTS.get(job.kind, "день"),
            date=f"{now:%d.%m.%Y}",
            facts="\n".join(f"- {part}" for part in parts),
        ), fallback=fallback)

        bus.publish(AGENT_MESSAGE, {"family_id": user.family_id, "user_ids": [user.id],
                                    "text": text, "severity": "info"})
        logger.info(f"Отправлена задача {job.kind} для {user.display_name}")


def run_reminders(db: Session, now: datetime):
    from app.modules.memory import reminders as reminders_service

    for reminder in reminders_service.due_reminders(db, now):
        user = db.get(User, reminder.user_id)
        if user is None:
            continue
        text = voice.speak(user, _REMINDER_HINT.format(text=reminder.text),
                           fallback=f"Напоминаю: {reminder.text}")
        bus.publish(AGENT_MESSAGE, {"family_id": user.family_id, "user_ids": [user.id],
                                    "text": text, "severity": "attention"})
        reminder.reminded_at = now
    db.commit()


def run_relationship_reviews(db: Session):
    """Фоновый разбор модуля «Подход»: раз в REVIEW_EVERY сообщений человека
    (см. app/modules/relationship/service.py) перечитывает разговор и
    обновляет заметки.

    Не больше REVIEW_BATCH_PER_TICK человек за тик: разбор — это ещё один
    LLM-вызов, потенциально небыстрый, а `tick()` общий и последовательный с
    `run_jobs`/`run_reminders`, у которых есть точные-по-минуте задачи
    (`_due` требует попадания в минуту без права навёрстывания). Тот, кто не
    попал в этот тик, останется «готов к разбору» и попадёт в следующий —
    порог messages не сгорает.

    `default=False` у `enabled_user_ids` намеренно: этот модуль не должен
    молча включаться тем, кто никогда его не просил (см. миграцию 0015).
    """
    from app.core.access import enabled_user_ids
    from app.modules.relationship import service

    processed = 0
    for user_id in enabled_user_ids(db, "relationship", default=False):
        if processed >= REVIEW_BATCH_PER_TICK:
            break
        if not service.due(db, user_id):
            continue
        user = db.get(User, user_id)
        if user is None:
            continue
        try:
            service.run_review(db, user)
        except Exception:
            logger.exception(f"Разбор подхода для {user.display_name} упал — пропускаю")
            db.rollback()
        processed += 1


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
        # После точных-по-минуте задач: разбор «Подхода» медленнее и не должен
        # мешать им попасть в свою минуту.
        run_relationship_reviews(db)
        if local.hour == RETENTION_HOUR and local.minute == 0:
            run_retention(db)


def prepare():
    """Загрузить модули и убедиться, что схема на месте.

    Планировщик и веб поднимаются одновременно, и рассчитывать на то, что схему
    успел создать кто-то другой, нельзя: проиграв гонку, планировщик раз в минуту
    спрашивал бы несуществующую таблицу и засыпал бы лог базы ошибками.
    `create_all` идемпотентен — кто пришёл первым, тот и создал.
    """
    load_modules()
    create_all()


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
