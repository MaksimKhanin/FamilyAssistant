"""Разбор разговора и ведение заметок о подходе.

Раз в REVIEW_EVERY новых сообщений человека планировщик (app/scheduler.py)
запускает `run_review`: один вызов модели с полным куском разговора с
прошлого разбора и текущим списком заметок — модель сама решает, что
добавить, что объединить и что снять. Так дедуп, мерж и ротация живут в
одном дисциплинированном месте, а не россыпью спонтанных remember/write_entry
посреди разговора — именно оттуда родился баг из трейса run 198, где одно и
то же осело пятью записями по трём доскам.

Ручного инструмента «записать заметку» в чате нет — только `review_approach`
(app/modules/relationship/tools.py), форсирующий тот же самый разбор.
"""
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.agent.llm import ROUTINE, LLMUnavailable, client as default_client
from app.core.logging import get_logger
from app.core.models import ChatMessage, User
from app.modules.memory import knowledge
from app.modules.memory.models import BoardEntry

logger = get_logger("relationship")

MODULE = "relationship"

#: Раз в сколько новых сообщений человека запускать разбор.
REVIEW_EVERY = 10

#: Ёмкость досок — та же забота, что у RULES_MAX: лента без предела
#: перестаёт быть обозримой и человеку, и recall. Обрезаются только записи
#: самого ассистента — то, что человек добавил руками, разбор не трогает.
NOTES_MAX = 50
SUMMARIES_MAX = 20

REVIEW_SYSTEM = """\
Ты ведёшь короткий внутренний профиль «что работает в общении с этим
человеком» — не для показа ему, а для того, чтобы ассистент точнее находил
подход в следующих разговорах.

Тебе даны уже сохранённые заметки (номер: текст) и кусок разговора с
прошлого разбора. Разбери его и верни один JSON-объект:

{
  "add": ["короткий устойчивый факт", "..."],
  "merge": [{"replaces": [номер, номер], "text": "объединённый факт"}],
  "remove": [{"id": номер, "reason": "почему снимаешь"}],
  "summary": "одно предложение о том, чем был этот кусок разговора",
  "digest": "3-5 строк — самое важное из текущих заметок, что должно быть под рукой всегда"
}

Правила:
- Заметка — устойчивый вывод (тон, что откликается, чего избегать), а не
  пересказ сцены и не выдуманная физическая деталь. Коротко, в третьем лице.
- Если новый вывод покрывает две и более существующих заметок — не добавляй
  их отдельно, объедини через merge и перечисли все номера, которые он
  заменяет.
- Если человек в этом куске явно возразил против чего-то из списка заметок
  или прямо сказал, что это не так, — сними её через remove с причиной.
- Ничего не выдумывай: не было нового — оставь "add"/"merge"/"remove"
  пустыми массивами, это нормальный ответ.
- "summary" — про сам разговор, одним предложением, без оценки человека.
- "digest" — не пересказ всего списка заметок, а то немногое, что важнее
  всего помнить прямо сейчас.
"""


def _cursor(db: Session, user_id: int):
    """Время последнего разбора — момент последней записи «Итогов
    разговоров», или None, если разбора ещё не было."""
    board = knowledge.approach_summaries_board(db, user_id, create=False)
    if board is None:
        return None
    entries = knowledge.list_entries(db, user_id, board.id)
    return entries[-1].created_at if entries else None


def pending_count(db: Session, user_id: int) -> int:
    """Сколько новых сообщений человека накопилось с последнего разбора."""
    since = _cursor(db, user_id)
    q = db.query(ChatMessage).filter(ChatMessage.user_id == user_id, ChatMessage.role == "user")
    if since is not None:
        q = q.filter(ChatMessage.created_at > since)
    return q.count()


def due(db: Session, user_id: int) -> bool:
    return pending_count(db, user_id) >= REVIEW_EVERY


def _notes_block(entries: Iterable[BoardEntry]) -> str:
    lines = [f"#{e.id}: {e.text}" for e in entries]
    return "\n".join(lines) if lines else "Заметок пока нет."


def _conversation_block(history: List[dict]) -> str:
    labels = {"user": "Человек", "assistant": "Ассистент"}
    lines = [f"{labels[m['role']]}: {m['content']}" for m in history if m.get("role") in labels]
    return "\n".join(lines)


def _enforce_cap(db: Session, user_id: int, board_id: int, cap: int) -> None:
    """Вытесняет самые старые записи ассистента сверх cap.

    Запись человека — не кандидат: `delete_assistant_entry` её и не тронет,
    поэтому доска вправе остаться больше cap, если человек сам её заполнил.
    """
    entries = knowledge.list_entries(db, user_id, board_id)  # старые сверху
    overflow = len(entries) - cap
    if overflow <= 0:
        return
    for entry in [e for e in entries if e.by_assistant][:overflow]:
        knowledge.delete_assistant_entry(db, user_id, entry.id)


def _apply(db: Session, user: User, raw: dict, known_ids: set) -> None:
    notes_board = knowledge.approach_notes_board(db, user.id, create=True)

    for item in raw.get("remove") or []:
        if not isinstance(item, dict):
            continue
        try:
            entry_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if entry_id in known_ids:
            knowledge.delete_assistant_entry(db, user.id, entry_id)

    for item in raw.get("merge") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        replaces = []
        for raw_id in item.get("replaces") or []:
            try:
                entry_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if entry_id in known_ids:
                replaces.append(entry_id)
        if not text or not replaces:
            continue
        deleted_any = False
        for entry_id in replaces:
            if knowledge.delete_assistant_entry(db, user.id, entry_id):
                deleted_any = True
        if deleted_any:
            knowledge.add_assistant_entry(db, user.id, notes_board.id, text)

    for text in raw.get("add") or []:
        text = str(text or "").strip()
        if text:
            knowledge.add_assistant_entry(db, user.id, notes_board.id, text)

    _enforce_cap(db, user.id, notes_board.id, NOTES_MAX)

    summary = str(raw.get("summary") or "").strip()
    if summary:
        summaries_board = knowledge.approach_summaries_board(db, user.id, create=True)
        knowledge.add_assistant_entry(db, user.id, summaries_board.id, summary)
        _enforce_cap(db, user.id, summaries_board.id, SUMMARIES_MAX)

    digest = str(raw.get("digest") or "").strip()
    if digest:
        # Своя строка, не памятка человека: digest раньше писался прямо в
        # memo «relationship» и затирал написанное руками. Теперь авто-выжимка
        # живёт под отдельным ключом, `instructions.for_prompt` возит обе.
        from app.core import instructions
        instructions.set_memo(db, user.id, instructions.auto_key(MODULE), digest)


def recent_summaries(db: Session, user_id: int, limit: int = 3) -> List[str]:
    """Последние итоги разговоров — свежие снизу, как в ленте доски.

    Это память ассистента о том, «про что мы вообще говорили», за пределами
    окна истории: сами итоги пишет `run_review`, а сюда за ними приходят
    системный промпт (тикет #77) и утренняя сводка за темой для follow-up.
    """
    board = knowledge.approach_summaries_board(db, user_id, create=False)
    if board is None:
        return []
    entries = knowledge.list_entries(db, user_id, board.id)
    return [e.text for e in entries[-limit:]]


def run_review(db: Session, user: User, llm=None) -> bool:
    """Один проход разбора для одного человека. True — разбор состоялся.

    Не зависит от того, набралось ли формально REVIEW_EVERY сообщений: это
    решает вызывающий (планировщик через `due`, или человек — по прямой
    просьбе через инструмент `review_approach`). Здесь — только сам разбор.
    """
    from app.agent.runtime import load_history

    since = _cursor(db, user.id)
    q = db.query(ChatMessage).filter(ChatMessage.user_id == user.id)
    if since is not None:
        q = q.filter(ChatMessage.created_at > since)
    # Запас на случай нескольких служебных строк, которые load_history
    # вставляет между репликами ассистента, — не отдельные строки
    # ChatMessage, но обрезать разговор по самому краю мы не хотим.
    limit = q.count() + 4
    if limit <= 4:
        return False

    history = load_history(db, user, limit=limit)
    conversation = _conversation_block(history)
    if not conversation.strip():
        return False

    notes_board = knowledge.approach_notes_board(db, user.id, create=True)
    entries = knowledge.list_entries(db, user.id, notes_board.id)
    known_ids = {e.id for e in entries}

    llm = llm or default_client
    try:
        raw = llm.json_completion(
            REVIEW_SYSTEM,
            f"Текущие заметки о подходе (номер: текст):\n{_notes_block(entries)}\n\n"
            f"Свежий кусок разговора с момента последнего разбора:\n{conversation}",
            task=ROUTINE,
        )
    except LLMUnavailable:
        logger.warning(f"Разбор подхода для {user.display_name} отложен — модель недоступна")
        return False

    if not isinstance(raw, dict):
        logger.warning(f"Разбор подхода для {user.display_name}: модель вернула не объект")
        return False

    _apply(db, user, raw, known_ids)
    return True
