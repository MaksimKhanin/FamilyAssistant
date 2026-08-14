"""Что человек рассказал ассистенту о себе и о том, кем ему быть.

Две вещи, обе свободным текстом и обе личные:

* **характер** — какую роль ассистент играет для этого человека. Один на человека
  и действует везде: это про манеру речи, а не про факты. «Неформальный, ироничный»
  или «отвечай сухо и по делу» — ответ на один и тот же вопрос будет разным по
  тону и одинаковым по цифрам. Не написал ничего — работает `DEFAULT_CHARACTER`,
  то самое «тепло и спокойно», с которым панель жила до всякой настройки;
* **памятка** — что учитывать в одной области. Про еду это желчный пузырь, гастрит
  и желание набрать вес; про дом — что по средам приходит уборщица. Памятка
  доезжает до модели только там, где её область в деле: незачем возить болячки
  в разбор кадра с камеры, а расписание уборщицы — в оценку тарелки супа.

Ничего из этого код не разбирает и не проверяет: человек пишет словами, модель
читает словами. Кода тут ровно на две заботы — не дать тексту разрастись до
размеров контекста и не показать его чужому.

Медицинского в памятке столько, сколько человек сам туда написал. Ассистент от
этого не становится врачом: правило «оценка, а не диагноз» живёт в тоне и памяткой
не отменяется — см. `app/agent/prompts.py`.
"""
from datetime import datetime
from typing import Dict, List, Tuple

from sqlalchemy.orm import Session

from app.core.models import ModuleMemo, User

#: Характер по умолчанию — тот, что до сих пор был написан в коде правилами тона.
#: Он тут не «настройка со значением», а обычный характер, просто не переписанный:
#: человек читает его на экране теми же словами, какими написал бы свой, и меняет
#: одним движением. Менять манеру ассистента правкой промпта больше не нужно —
#: правится эта строка.
DEFAULT_CHARACTER = (
    "тепло и спокойно, как внимательный член семьи, а не как система мониторинга "
    "и не как корпоративный отчёт; коротко — одно-два предложения, если не просят "
    "подробностей; о доме без драматизма"
)

#: Сколько символов человек может написать. Ограничение не про безопасность, а про
#: контекст: памятки уезжают в каждый запрос своей области, и полотно на страницу
#: вытеснит из окна историю разговора.
CHARACTER_LIMIT = 700
MEMO_LIMIT = 1200


def _clean(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


# --- характер -------------------------------------------------------------

def character(user: User) -> str:
    """Характер, с которым ассистент пойдёт отвечать: свой или умолчание.

    Пустого характера не бывает — говорить как-то всё равно надо, и раз манеры в
    коде больше нет, пустое поле означает «меня устраивает то, что было».
    """
    return own_character(user) or DEFAULT_CHARACTER


def own_character(user: User) -> str:
    """То, что человек написал сам, — для экрана: там пустое поле и умолчание в
    подсказке честнее, чем предзаполненный текст, который страшно стереть."""
    return (user.assistant_character or "").strip()


def set_character(db: Session, user: User, text: str) -> str:
    """Задать характер. Пустая строка — вернуться к умолчанию."""
    user.assistant_character = _clean(text, CHARACTER_LIMIT) or None
    db.commit()
    return character(user)


# --- памятки --------------------------------------------------------------

def memos(db: Session, user_id: int) -> Dict[str, str]:
    """Непустые памятки человека: имя модуля → текст."""
    rows = db.query(ModuleMemo).filter(ModuleMemo.user_id == user_id).all()
    return {row.module: row.text.strip() for row in rows if (row.text or "").strip()}


def memo(db: Session, user_id: int, module: str) -> str:
    """Памятка одной области — пустая строка, если человек ничего не писал."""
    return memos(db, user_id).get(module, "")


def set_memo(db: Session, user_id: int, module: str, text: str) -> str:
    """Сохранить памятку. Пустой текст стирает строку, а не хранит пустую."""
    row = (
        db.query(ModuleMemo)
        .filter(ModuleMemo.user_id == user_id, ModuleMemo.module == module)
        .one_or_none()
    )
    cleaned = _clean(text, MEMO_LIMIT)

    if not cleaned:
        if row is not None:
            db.delete(row)
            db.commit()
        return ""

    if row is None:
        row = ModuleMemo(user_id=user_id, module=module)
        db.add(row)
    row.text = cleaned
    row.updated_at = datetime.utcnow()
    db.commit()
    return cleaned


# --- то, что уходит в промпт ----------------------------------------------

def for_prompt(db: Session, user_id: int, modules: List[str]) -> List[Tuple[str, str]]:
    """Памятки перечисленных областей — парами «название модуля, текст».

    `modules` приходит из того, что человеку включено, поэтому памятка выключенного
    модуля молча остаётся лежать: человек её не терял, но и в контекст она не едет.
    """
    from app.modules import by_name

    known = by_name()
    written = memos(db, user_id)
    pairs = []
    for name in modules:
        text = written.get(name)
        if not text:
            continue
        module = known.get(name)
        pairs.append((module.title if module else name, text))
    return pairs


def memo_modules(db: Session, user_id: int, modules: List[str]) -> List[dict]:
    """Строки для экрана: у каких областей есть куда написать памятку.

    Памятку заводит не всякий модуль, а тот, кто объяснил, о чём в ней писать
    (`Module.memo_hint`) — иначе человек смотрит на пустое поле и не понимает,
    чего от него хотят.
    """
    from app.modules import by_name

    known = by_name()
    written = memos(db, user_id)
    rows = []
    for name in modules:
        module = known.get(name)
        if module is None or not module.memo_hint:
            continue
        rows.append({
            "name": module.name,
            "title": module.title,
            "hint": module.memo_hint,
            "text": written.get(name, ""),
        })
    return rows
