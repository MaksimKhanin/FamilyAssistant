"""Первый вход: семья и глава семьи заводятся из окружения.

Так администратор задаётся один раз в `.env` при развёртывании, а дальше учётные
записи семьи он нарезает в панели — без консоли и без SQL.

Правила, ради которых это не просто «создать пользователя»:

  * **идемпотентно**. Функция вызывается при каждом старте; если люди в базе уже
    есть, она молчит и ничего не трогает;
  * **пароль из окружения применяется только к пустой базе**. Иначе смена пароля
    в интерфейсе откатывалась бы при каждом рестарте контейнера;
  * **есть аварийный выход**. `ADMIN_PASSWORD_RESET=1` вернёт пароль главы к
    значению из окружения — на случай, если его забыли.
"""
from typing import Optional

from sqlalchemy.orm import Session

from app.core.auth import hash_password
from app.core.config import settings
from app.core.family import get_settings
from app.core.logging import get_logger
from app.core.models import ROLE_HEAD, Family, User

logger = get_logger("bootstrap")


def ensure_admin(db: Session) -> Optional[User]:
    """Завести семью и главу семьи, если база ещё пустая. Возвращает главу или None."""
    admin_cfg = settings.admin

    existing = db.query(User).filter(User.username == admin_cfg.username).one_or_none()
    if existing is not None:
        if admin_cfg.reset_password and admin_cfg.password:
            existing.password_hash = hash_password(admin_cfg.password)
            existing.role = ROLE_HEAD
            db.commit()
            logger.warning(f"Пароль главы семьи «{existing.username}» сброшен из окружения. "
                           f"Снимите ADMIN_PASSWORD_RESET, чтобы это не повторялось.")
        return existing

    if db.query(User).count():
        # Люди уже есть, просто под другими именами — не вмешиваемся.
        return None

    if not admin_cfg.configured:
        logger.warning("В базе нет ни одного пользователя, а ADMIN_USERNAME/ADMIN_PASSWORD "
                       "не заданы — войти будет некому. Заполните их в .env и перезапустите.")
        return None

    family = db.query(Family).order_by(Family.id).first()
    if family is None:
        family = Family(name=admin_cfg.family_name)
        db.add(family)
        db.flush()

    admin = User(
        family_id=family.id,
        username=admin_cfg.username,
        password_hash=hash_password(admin_cfg.password),
        display_name=admin_cfg.display_name or admin_cfg.username,
        relation=admin_cfg.relation or None,
        role=ROLE_HEAD,
        avatar_slot=0,
        autonomy=1,
    )
    db.add(admin)
    db.flush()
    get_settings(db, family.id)
    db.commit()

    logger.info(f"Создана семья «{family.name}» и глава семьи «{admin.username}». "
                f"Остальных заведите в панели: Настройки → Семья и модули.")
    return admin
