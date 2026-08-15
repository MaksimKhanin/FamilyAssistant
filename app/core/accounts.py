"""Учётные записи: кого заводим, кому выдаём вход, кого убираем.

Первый администратор появляется из окружения при первом старте
(`app/core/bootstrap.py`), всё остальное делается здесь и вызывается из панели.
Правила простые, но их лучше держать в одном месте, а не размазывать по роутам:

  * заводить, переименовывать и удалять людей может только администратор;
  * себя нельзя ни удалить, ни разжаловать — иначе легко остаться без входа;
  * последний администратор не может перестать быть администратором по той же причине;
  * ссылка-приглашение одноразовая, и её перевыпуск — он же сброс пароля:
    старая ссылка и старый пароль сразу перестают работать.

Роль — это вся учётка целиком, а не набор галочек: администратор ассистентом не
пользуется, участник настроек не трогает (ADR-0008). Поэтому «сделать участника
администратором» — операция редкая и заметная: человек теряет доступ к своему
разговору и своим модулям, хотя записи никуда не деваются.

Нарушение правила — это `AccountError` с фразой, которую не стыдно показать
человеку: интерфейс печатает её как есть.
"""
import secrets
from typing import List, Optional

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.models import ROLE_ADMIN, ROLE_MEMBER, User

logger = get_logger("accounts")

#: Ограничение на участников семьи. Админские учётки в него не входят: это не
#: люди за столом, а служебный вход, и запирать из-за них семью незачем.
MAX_MEMBERS = 12          # семья, а не корпоративный каталог
AVATAR_SLOTS = 5


class AccountError(RuntimeError):
    """Действие не разрешено или невозможно. Текст показывается человеку."""


def new_invite_code() -> str:
    return secrets.token_urlsafe(9)


# --- проверки -------------------------------------------------------------

def _require_admin(actor: User):
    if not actor.is_admin:
        raise AccountError("Учётные записи заводит и убирает администратор.")


def _same_family(actor: User, target: User) -> User:
    if target is None or target.family_id != actor.family_id:
        raise AccountError("Такого человека нет в вашей семье.")
    return target


def _fetch(db: Session, actor: User, user_id: int) -> User:
    return _same_family(actor, db.get(User, user_id))


def administrators(db: Session, family_id: int) -> List[User]:
    return db.query(User).filter(User.family_id == family_id, User.role == ROLE_ADMIN).all()


# --- создание -------------------------------------------------------------

def username_from(display_name: str) -> str:
    """Логин из имени: «Лёва» → «leva». Латиница, потому что его придётся вводить."""
    translit = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    slug = display_name.strip().lower().translate(translit)
    slug = "".join(ch for ch in slug if ch.isalnum())
    return slug[:32] or "member"


def _unique_username(db: Session, base: str) -> str:
    username = base
    suffix = 2
    while db.query(User).filter(User.username == username).one_or_none() is not None:
        username = f"{base}{suffix}"
        suffix += 1
    return username


def create_member(db: Session, actor: User, display_name: str, relation: str = "") -> User:
    """Завести участника. Пароль он придумает сам по ссылке-приглашению."""
    _require_admin(actor)

    display_name = (display_name or "").strip()
    if not display_name:
        raise AccountError("Без имени не получится — как к человеку обращаться?")

    members = (
        db.query(User)
        .filter(User.family_id == actor.family_id, User.role == ROLE_MEMBER)
        .all()
    )
    if len(members) >= MAX_MEMBERS:
        raise AccountError(f"Больше {MAX_MEMBERS} человек в одной семье — это уже не семья.")

    member = User(
        family_id=actor.family_id,
        username=_unique_username(db, username_from(display_name)),
        display_name=display_name[:64],
        relation=(relation or "").strip()[:32] or None,
        role=ROLE_MEMBER,
        avatar_slot=len(members) % AVATAR_SLOTS,
        invite_code=new_invite_code(),
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    logger.info(f"Заведён участник {member.username} ({member.display_name})")
    return member


# --- изменение ------------------------------------------------------------

def rename(db: Session, actor: User, user_id: int, display_name: str, relation: str = "") -> User:
    _require_admin(actor)
    member = _fetch(db, actor, user_id)

    display_name = (display_name or "").strip()
    if not display_name:
        raise AccountError("Имя не может быть пустым.")

    member.display_name = display_name[:64]
    member.relation = (relation or "").strip()[:32] or None
    db.commit()
    return member


def set_admin(db: Session, actor: User, user_id: int, is_admin: bool) -> User:
    """Сделать учётку административной или вернуть её в участники.

    Смена роли меняет учётку целиком: администратор теряет разговор, модули и
    свои настройки, участник — админ-раздел. Данные при этом остаются на месте,
    поэтому операция обратима: вернули роль — вернулись и записи.
    """
    _require_admin(actor)
    member = _fetch(db, actor, user_id)

    # Своя роль не меняется. Отдельной проверки «последний администратор» не
    # нужно: менять роли может только админ, и если он один — это он и есть.
    if member.id == actor.id:
        raise AccountError("Свою роль изменить нельзя — попросите другого администратора.")

    member.role = ROLE_ADMIN if is_admin else ROLE_MEMBER
    db.commit()
    logger.info(f"{member.username}: роль → {member.role}")
    return member


def issue_invite(db: Session, actor: User, user_id: int) -> User:
    """Выдать (или перевыпустить) ссылку-приглашение.

    Это же и сброс пароля: старый пароль перестаёт работать, человек заходит по
    ссылке и придумывает новый. Отдельной кнопки «сбросить пароль» нет — она бы
    делала ровно то же самое.
    """
    _require_admin(actor)
    member = _fetch(db, actor, user_id)

    # Себе — нельзя: это стёрло бы собственный пароль, и, если не скопировать
    # ссылку немедленно, администратор остался бы снаружи собственной панели.
    if member.id == actor.id:
        raise AccountError("Свой пароль меняйте в профиле — так вы точно не потеряете вход.")

    member.invite_code = new_invite_code()
    member.password_hash = None
    db.commit()
    logger.info(f"{member.username}: выпущено новое приглашение, старый пароль сброшен")
    return member


def change_own_password(db: Session, actor: User, current_password: str, new_password: str,
                        repeat: str, min_length: int = 6) -> User:
    """Сменить пароль себе — единственный способ, не требующий чужой помощи."""
    from app.core.auth import hash_password, verify_password

    if not verify_password(current_password or "", actor.password_hash):
        raise AccountError("Текущий пароль не подошёл.")
    if len(new_password or "") < min_length:
        raise AccountError(f"Новый пароль короче {min_length} символов — придумайте подлиннее.")
    if new_password != repeat:
        raise AccountError("Новые пароли не совпали.")

    actor.password_hash = hash_password(new_password)
    actor.invite_code = None      # выданная кем-то ссылка больше не нужна
    db.commit()
    logger.info(f"{actor.username}: пароль изменён самим человеком")
    return actor


def revoke_invite(db: Session, actor: User, user_id: int) -> User:
    """Отозвать неиспользованную ссылку, ничего не меняя у тех, кто уже вошёл."""
    _require_admin(actor)
    member = _fetch(db, actor, user_id)
    member.invite_code = None
    db.commit()
    return member


def delete_member(db: Session, actor: User, user_id: int) -> str:
    """Убрать человека вместе со всеми его записями.

    Данные уезжают каскадом по внешним ключам: еда, активность, знания, диалог,
    подписки на уведомления. Это необратимо, поэтому интерфейс переспрашивает.
    """
    _require_admin(actor)
    member = _fetch(db, actor, user_id)

    # Отдельной проверки «последний администратор» здесь не нужно: удалять может
    # только админ, а единственный админ — это он сам, и его останавливает строка ниже.
    if member.id == actor.id:
        raise AccountError("Себя удалить нельзя. Попросите другого администратора.")

    name = member.display_name
    db.delete(member)
    db.commit()
    logger.info(f"Удалён участник {name}")
    return name


# --- сведения для экрана --------------------------------------------------

def status_of(member: User) -> str:
    if member.password_hash:
        return "заходит сам"
    if member.invite_code:
        return "ждёт приглашения"
    return "нет доступа"


def overview(db: Session, actor: User) -> List[dict]:
    """Строки для админского экрана «Учётные записи» — все учётки, включая свою."""
    accounts_rows = db.query(User).filter(User.family_id == actor.family_id).order_by(User.id).all()
    #: Над собой администратор не властен: ни удалить, ни сменить роль — иначе
    #: единственный админ может случайно запереть себя снаружи.
    manageable = lambda member: actor.is_admin and member.id != actor.id   # noqa: E731
    return [{
        "user": member,
        "status": status_of(member),
        "can_delete": manageable(member),
        "can_change_role": manageable(member),
    } for member in accounts_rows]


def find_by_invite(db: Session, code: str) -> Optional[User]:
    return db.query(User).filter(User.invite_code == code).one_or_none() if code else None
