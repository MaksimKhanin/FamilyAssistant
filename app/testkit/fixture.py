"""Известная база: то же самое состояние перед каждым прогоном сценария.

Сценарий, который начинает с чужих данных, проверяет не поведение, а везение:
вчерашняя доска с тем же именем — и «создай доску» отвечает отказом, вчерашние
приёмы пищи — и «сколько сегодня» показывает не то. Поэтому прогон начинается с
`reset`: база сносится и собирается заново из этого файла.

Семья тут маленькая и намеренно скучная: администратор, двое участников и по
одному предмету на каждый модуль — раздел с доской, камера с ночным событием,
приём пищи за сегодня. Всё, что сложнее, сценарий доводит сам — своими же
ручками, теми, которыми пользуется человек: так проверяется путь, а не заготовка.

Пароль у всех один и заведомо слабый — это стенд, и наружу он не смотрит.
"""
from datetime import datetime, timedelta
from typing import Dict, List

from sqlalchemy.orm import Session

from app.agent import policy, tracing
from app.core.auth import hash_password
from app.core.clock import local_now, to_utc
from app.core.db import Base, SessionLocal, engine
from app.core.logging import get_logger
from app.core.models import ROLE_ADMIN, ROLE_MEMBER, Family, User

logger = get_logger("testkit")

PASSWORD = "test12345"

#: Кто живёт на стенде. Марина — тот, за кого ходят сценарии; Лёва нужен ровно
#: для одного вопроса, который иначе не задать: видно ли одному участнику чужое.
PEOPLE = [
    {"username": "marina", "display_name": "Марина", "relation": "мама", "role": ROLE_MEMBER,
     "avatar_slot": 0},
    {"username": "leva", "display_name": "Лёва", "relation": "сын", "role": ROLE_MEMBER,
     "avatar_slot": 1},
    {"username": "admin", "display_name": "Администратор", "relation": None, "role": ROLE_ADMIN,
     "avatar_slot": 4},
]


def reset(autonomy: int = 2, traces: bool = True, seed: bool = True) -> Dict:
    """Снести базу и собрать заново. Возвращает описание того, что получилось."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        family = Family(name="Стенд")
        db.add(family)
        db.flush()

        users = {}
        for person in PEOPLE:
            user = User(family_id=family.id, password_hash=hash_password(PASSWORD), **person)
            db.add(user)
            db.flush()
            users[user.username] = user
        db.commit()

        policy.set_autonomy(db, family.id, autonomy)
        # Трейсы включены по умолчанию: сценарий без них видит ответ, но не видит,
        # чем он получился, — а разбирать придётся именно это.
        settings_row = tracing.get_settings(db, family.id)
        settings_row.enabled = bool(traces)
        db.commit()

        if seed:
            _seed(db, users)

        return describe(db)
    finally:
        db.close()


def _seed(db: Session, users: Dict[str, User]):
    """По одному предмету на модуль — чтобы экраны были не пустыми."""
    from app.modules.memory import knowledge, reminders
    from app.modules.nutrition import models as nutrition_models
    from app.modules.security import models as security_models

    marina = users["marina"]
    now = local_now()

    section = knowledge.create_section(db, marina.id, "Дом")
    board = knowledge.create_board(db, marina.id, section.id, "Счётчики",
                                   instruction="Записи вида «вода 123» — показание счётчика.")
    # Словаря величин у доски нет, поэтому запись не поедет на разбор в модель:
    # сборка стенда не должна зависеть от того, отвечает ли она сейчас.
    knowledge.add_entry(db, marina.id, board.id, "вода 123")

    # Напоминание в прошлом: наступившее время — единственный способ проверить
    # рассылку, не переводя часы (см. ручку `tick`).
    reminders.add_reminder(db, marina.id, "выпить лекарство", to_utc(now - timedelta(minutes=5)))

    db.add(nutrition_models.Meal(
        user_id=marina.id, title="овсянка с ягодами", kcal=320, protein=9, fat=7, carbs=54,
        status=nutrition_models.STATUS_CONFIRMED, source=nutrition_models.SOURCE_TEXT,
        confidence="high", raw_input="овсянка с ягодами",
        eaten_at=to_utc(now - timedelta(hours=3)),
    ))

    camera = security_models.Camera(family_id=marina.family_id, slug="gate", label="Калитка",
                                    zone="улица")
    db.add(camera)
    db.flush()
    db.add(security_models.SecurityEvent(
        family_id=marina.family_id, camera_id=camera.id,
        detected_class="person", reason="Человек у калитки ночью",
        verdict=security_models.VERDICT_ANOMALY, classified_by="rules",
        happened_at=to_utc(now - timedelta(hours=8)),
    ))
    db.commit()


def describe(db: Session) -> Dict:
    """Кто есть на стенде — сценарию, чтобы знать, за кого ходить."""
    rows: List[dict] = []
    for user in db.query(User).order_by(User.id).all():
        rows.append({"id": user.id, "username": user.username, "name": user.display_name,
                     "role": user.role, "family_id": user.family_id})
    return {"password": PASSWORD, "users": rows}
