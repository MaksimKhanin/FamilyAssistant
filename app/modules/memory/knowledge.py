"""Сервис знаний: разделы → доски → записи (спека #19).

Пока здесь живут только разделы (#25). Единая точка разрешения доступа к
доскам — «какие доски видит этот участник и с каким правом» — появится вместе
с шарингом (#28); ходить к доскам мимо неё будет нельзя.

Раздел строго личный: каждая функция принимает `user_id` смотрящего и не
отдаёт и не трогает чужого. Проверка владельца здесь, а не в маршрутах,
чтобы у экрана и будущих инструментов агента была одна и та же граница.
"""
from typing import List, Optional

from sqlalchemy.orm import Session

from app.modules.memory.models import Section


def list_sections(db: Session, user_id: int) -> List[Section]:
    """Полоса разделов: закреплённые впереди, дальше по свежести."""
    return (
        db.query(Section)
        .filter(Section.user_id == user_id)
        .order_by(Section.pinned.desc(), Section.last_activity_at.desc(), Section.id.desc())
        .all()
    )


def get_section(db: Session, user_id: int, section_id: int) -> Optional[Section]:
    section = db.get(Section, section_id)
    return section if section is not None and section.user_id == user_id else None


#: Столбец `sections.name` — String(128): SQLite длину не проверяет, Postgres
#: падает, поэтому лишнее отрезается здесь.
NAME_LIMIT = 128


def create_section(db: Session, user_id: int, name: str) -> Optional[Section]:
    name = name.strip()[:NAME_LIMIT]
    if not name:
        return None
    section = Section(user_id=user_id, name=name)
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def rename_section(db: Session, user_id: int, section_id: int, name: str) -> Optional[Section]:
    section = get_section(db, user_id, section_id)
    name = name.strip()[:NAME_LIMIT]
    if section is None or not name:
        return None
    section.name = name
    db.commit()
    return section


def toggle_pin(db: Session, user_id: int, section_id: int) -> Optional[Section]:
    section = get_section(db, user_id, section_id)
    if section is None:
        return None
    section.pinned = not section.pinned
    db.commit()
    return section


def delete_section(db: Session, user_id: int, section_id: int) -> bool:
    """Удаление раздела — каскад: раздел → доски → записи.

    Блокировка «в разделе есть доска с активным доступом» появится вместе с
    шарингом (#28); до него любой свой раздел удаляется свободно.
    """
    section = get_section(db, user_id, section_id)
    if section is None:
        return False
    db.delete(section)
    db.commit()
    return True
