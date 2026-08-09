"""Family-level helpers: members, settings, the head of the family."""
import json
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core.models import Family, FamilySettings, ROLE_HEAD, User

#: Sources the family can put into the shared knowledge base («Модель и знания»).
RAG_SOURCES = {
    "recipes": "Рецепты и вкусы семьи",
    "nutrition_history": "История питания",
    "home_map": "План дома и зоны камер",
    "restrictions": "Ограничения и аллергии",
    "calendar": "Семейный календарь",
}


def members(db: Session, family_id: int) -> List[User]:
    return db.query(User).filter(User.family_id == family_id).order_by(User.id).all()


def head_of_family(db: Session, family_id: int) -> Optional[User]:
    return (
        db.query(User)
        .filter(User.family_id == family_id, User.role == ROLE_HEAD)
        .order_by(User.id)
        .first()
    )


def get_settings(db: Session, family_id: int) -> FamilySettings:
    row = db.query(FamilySettings).filter(FamilySettings.family_id == family_id).one_or_none()
    if row is None:
        row = FamilySettings(
            family_id=family_id,
            rag_sources_json=json.dumps({key: True for key in RAG_SOURCES}, ensure_ascii=False),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def rag_sources(settings_row: FamilySettings) -> Dict[str, bool]:
    try:
        stored = json.loads(settings_row.rag_sources_json or "{}")
    except ValueError:
        stored = {}
    return {key: bool(stored.get(key, True)) for key in RAG_SOURCES}


def set_rag_sources(db: Session, settings_row: FamilySettings, values: Dict[str, bool]):
    settings_row.rag_sources_json = json.dumps(
        {key: bool(values.get(key, False)) for key in RAG_SOURCES}, ensure_ascii=False
    )
    db.commit()


def rename(db: Session, family: Family, name: str):
    family.name = name.strip()[:128] or family.name
    db.commit()
