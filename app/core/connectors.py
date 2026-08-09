"""External service connections — personal for each family member.

The catalogue is declarative on purpose: a connector is a name, a description and
a permission level. Actually speaking to Google Calendar or a grocery service is a
later step; what the MVP fixes now is the *contract* — the family decides, per
person and per service, whether the assistant only reads, prepares actions for
confirmation, or acts on its own.
"""
from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy.orm import Session

from app.core.models import CONN_ACT, CONN_CONFIRM, CONN_OFF, CONN_READ, Connector

PERMISSION_LABELS = {
    CONN_READ: "Только читает",
    CONN_CONFIRM: "Пишет с подтверждением",
    CONN_ACT: "Действует сам",
    CONN_OFF: "Выключен",
}

PERMISSION_EXPLANATIONS = {
    CONN_READ: "Читает данные, ничего не меняет.",
    CONN_CONFIRM: "Готовит действие и показывает вам перед отправкой.",
    CONN_ACT: "Делает сам и пишет об этом в ленте действий.",
    CONN_OFF: "Доступ закрыт — агент туда не ходит.",
}


@dataclass(frozen=True)
class ConnectorSpec:
    service: str
    label: str
    description: str


CATALOG: List[ConnectorSpec] = [
    ConnectorSpec("telegram", "Telegram", "Тот же диалог с ассистентом, что и в панели"),
    ConnectorSpec("gcal", "Google Календарь", "Видит планы семьи, чтобы не предлагать невпопад"),
    ConnectorSpec("mail", "Почта", "Замечает важные письма и напоминает о них"),
    ConnectorSpec("notes", "Заметки", "Складывает списки и идеи туда, где вы их ищете"),
    ConnectorSpec("grocery", "Доставка продуктов", "Собирает корзину по плану питания"),
    ConnectorSpec("whatsapp", "WhatsApp", "Ещё один канал для сообщений семье"),
    ConnectorSpec("social", "Соцсети", "Смотрит за упоминаниями семьи и детей"),
]


def rows_for(db: Session, user_id: int) -> Dict[str, Connector]:
    return {c.service: c for c in db.query(Connector).filter(Connector.user_id == user_id)}


def overview(db: Session, user_id: int) -> List[dict]:
    existing = rows_for(db, user_id)
    result = []
    for spec in CATALOG:
        row = existing.get(spec.service)
        permission = row.permission if row else CONN_OFF
        result.append({
            "spec": spec,
            "connected": bool(row and row.connected),
            "permission": permission,
            "permission_text": PERMISSION_EXPLANATIONS[permission],
        })
    return result


def set_permission(db: Session, user_id: int, service: str, permission: str):
    if permission not in PERMISSION_LABELS:
        raise ValueError(f"Неизвестный уровень доступа: {permission}")
    row = _get_or_create(db, user_id, service)
    row.permission = permission
    db.commit()


def set_connected(db: Session, user_id: int, service: str, connected: bool):
    row = _get_or_create(db, user_id, service)
    row.connected = connected
    # Начинать всегда стоит с «только читает» — так безопаснее и понятнее.
    row.permission = CONN_READ if connected and row.permission == CONN_OFF else (
        CONN_OFF if not connected else row.permission
    )
    db.commit()


def _get_or_create(db: Session, user_id: int, service: str) -> Connector:
    if service not in {spec.service for spec in CATALOG}:
        raise ValueError(f"Нет такого коннектора: {service}")
    row = (
        db.query(Connector)
        .filter(Connector.user_id == user_id, Connector.service == service)
        .one_or_none()
    )
    if row is None:
        row = Connector(user_id=user_id, service=service, connected=False, permission=CONN_OFF)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row
