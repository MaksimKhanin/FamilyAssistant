"""Список покупок: положить, показать, вычеркнуть, убрать вычеркнутое.

Дедупликация — против невычеркнутого и без учёта регистра: «Молоко» второй раз
не ложится, а вот вычеркнутое вчера молоко можно положить снова.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.modules.shopping.models import ShoppingItem

#: Сколько знаков живёт в одной строке списка.
ITEM_LIMIT = 120
#: Не больше стольких позиций кладётся за один вызов — «весь рецепт» это
#: десяток строк, а не сотня.
BATCH_LIMIT = 20
#: Сколько дней вычеркнутое остаётся видимым, прежде чем ночная уборка его снимет.
CHECKED_RETENTION_DAYS = 3


def list_items(db: Session, family_id: int) -> List[ShoppingItem]:
    """Невычеркнутое первым, внутри — свежее снизу, как писали."""
    return (
        db.query(ShoppingItem)
        .filter(ShoppingItem.family_id == family_id)
        .order_by(ShoppingItem.checked, ShoppingItem.id)
        .all()
    )


def open_items(db: Session, family_id: int) -> List[ShoppingItem]:
    return [item for item in list_items(db, family_id) if not item.checked]


def add_items(db: Session, family_id: int, user_id: Optional[int],
              texts: List[str]) -> Tuple[List[ShoppingItem], List[str]]:
    """Положить позиции. Возвращает (новые, уже лежавшие)."""
    existing = {item.text.strip().lower() for item in open_items(db, family_id)}
    added: List[ShoppingItem] = []
    duplicates: List[str] = []
    for raw in texts[:BATCH_LIMIT]:
        text = (raw or "").strip()[:ITEM_LIMIT]
        if not text:
            continue
        key = text.lower()
        if key in existing:
            duplicates.append(text)
            continue
        item = ShoppingItem(family_id=family_id, added_by=user_id, text=text)
        db.add(item)
        added.append(item)
        existing.add(key)
    if added:
        db.commit()
        for item in added:
            db.refresh(item)
    return added, duplicates


def check_off(db: Session, family_id: int, name: str) -> Optional[ShoppingItem]:
    """Вычеркнуть по имени — сначала точное совпадение, потом по подстроке.

    Неоднозначную подстроку («сыр» при «сыр твёрдый» и «сырки») не угадываем:
    честнее вернуть None и дать ассистенту переспросить.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return None
    items = open_items(db, family_id)
    exact = [item for item in items if item.text.strip().lower() == needle]
    partial = [item for item in items if needle in item.text.strip().lower()]
    match = exact[0] if exact else (partial[0] if len(partial) == 1 else None)
    if match is None:
        return None
    match.checked = True
    match.checked_at = datetime.utcnow()
    db.commit()
    return match


def toggle(db: Session, family_id: int, item_id: int) -> Optional[ShoppingItem]:
    """Тумблер строки с экрана: вычеркнуть или вернуть."""
    item = db.get(ShoppingItem, item_id)
    if item is None or item.family_id != family_id:
        return None
    item.checked = not item.checked
    item.checked_at = datetime.utcnow() if item.checked else None
    db.commit()
    return item


def delete_item(db: Session, family_id: int, item_id: int) -> bool:
    item = db.get(ShoppingItem, item_id)
    if item is None or item.family_id != family_id:
        return False
    db.delete(item)
    db.commit()
    return True


def purge_checked(db: Session, now: datetime = None) -> int:
    """Ночная уборка: вычеркнутое старше CHECKED_RETENTION_DAYS уходит само."""
    cutoff = (now or datetime.utcnow()) - timedelta(days=CHECKED_RETENTION_DAYS)
    removed = (
        db.query(ShoppingItem)
        .filter(ShoppingItem.checked.is_(True), ShoppingItem.checked_at < cutoff)
        .delete()
    )
    if removed:
        db.commit()
    return removed
