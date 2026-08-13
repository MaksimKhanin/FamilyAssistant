"""Переезд заметок на доски и в напоминания (тикет #33, спека #19).

Плоские заметки были одной кучей с четырьмя ярлыками. Знания устроены иначе:
раздел → доска → запись, — поэтому каждый ярлык становится доской в разделе
«Личное», а «напоминание» вообще уходит из знаний в свою таблицу.

Три решения, которые здесь стоит объяснить:

* **Расплывчатый срок не становится напоминанием.** «В пятницу утром» — это не
  момент, и выдумывать за человека пятницу нельзя (спека #19: без времени
  напоминание не заводится). Такая заметка переезжает записью, сохранив свои
  слова: «купить корм (когда-то: в пятницу утром)».
* **Автор берётся из `source`.** «Добавлено вручную» писал человек, «из
  разговора…» — ассистент. На доске это разные авторы, и сваливать всё на
  человека значило бы приписать ему слова ассистента (ADR-0004).
* **Старая таблица переименовывается, а не удаляется.** Перенос разбирает
  свободный текст, и человеку должно остаться куда посмотреть.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Единственный раздел переезда: всё перенесённое — личное.
SECTION = "Личное"

#: Вид заметки → доска, на которую он переезжает, вместе с инструкцией ассистенту:
#: доска без инструкции — это лента, которую ассистент читает наугад.
BOARDS = {
    "pref": ("Предпочтения",
             "Что человек любит и чего не ест: вкусы, привычки, мелочи быта. "
             "Учитывай, когда советуешь."),
    "health": ("Здоровье",
               "Ограничения и наблюдения о здоровье: аллергии, лекарства, самочувствие. "
               "Это не медицинские факты — говори о них осторожно."),
    "fact": ("Наблюдения",
             "Всё, что ассистент запомнил из разговоров и что не легло в другие доски."),
}

#: Куда переезжает «напоминание», у которого не оказалось времени: оно не
#: напоминание, а наблюдение со словами человека о сроке.
FALLBACK_KIND = "fact"

#: Источник, по которому видно человеческую руку. Остальное писал ассистент.
BY_HAND = "добавлено вручную"


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()
    # База, развёрнутая `create_all()` из моделей, заметок не знает вовсе:
    # notes_legacy у неё уже на месте и пуст — переносить нечего.
    if "notes" not in tables:
        return

    _move_notes(bind)
    op.rename_table("notes", "notes_legacy")


def downgrade() -> None:
    # Обратно едет только таблица: разложенное по доскам не собирается назад в
    # кучу — записи с тех пор уже правили руками.
    op.rename_table("notes_legacy", "notes")


# --- перенос ---------------------------------------------------------------------

def _scalar(bind, sql: str, **params):
    return bind.execute(sa.text(sql), params).scalar()


def _section_id(bind, user_id: int, at) -> int:
    """Раздел «Личное» этого человека — свой, если он уже завёлся под «Память
    ассистента», иначе новый."""
    found = _scalar(bind, "SELECT id FROM sections WHERE user_id = :user_id AND name = :name "
                          "ORDER BY id LIMIT 1", user_id=user_id, name=SECTION)
    if found is not None:
        return found
    bind.execute(sa.text(
        "INSERT INTO sections (user_id, name, pinned, last_activity_at, created_at) "
        "VALUES (:user_id, :name, :pinned, :at, :at)"),
        {"user_id": user_id, "name": SECTION, "pinned": False, "at": at})
    return _scalar(bind, "SELECT id FROM sections WHERE user_id = :user_id AND name = :name "
                         "ORDER BY id DESC LIMIT 1", user_id=user_id, name=SECTION)


def _board_id(bind, section_id: int, name: str, instruction: str, at) -> int:
    found = _scalar(bind, "SELECT id FROM boards WHERE section_id = :section_id AND name = :name "
                          "ORDER BY id LIMIT 1", section_id=section_id, name=name)
    if found is not None:
        return found
    bind.execute(sa.text(
        "INSERT INTO boards (section_id, name, instruction, share_all, share_all_right, "
        "                    last_activity_at, created_at) "
        "VALUES (:section_id, :name, :instruction, :share_all, NULL, :at, :at)"),
        {"section_id": section_id, "name": name, "instruction": instruction,
         "share_all": False, "at": at})
    return _scalar(bind, "SELECT id FROM boards WHERE section_id = :section_id AND name = :name "
                         "ORDER BY id DESC LIMIT 1", section_id=section_id, name=name)


def _entry_text(note) -> str:
    """Слова заметки — и её собственные слова о сроке, если срока разобрать не вышло.

    Сюда приходит только то, что не стало напоминанием, поэтому уцелевший
    `when_text` — как раз тот расплывчатый срок, который выдумывать нельзя.
    """
    text = (note["text"] or "").strip()
    when = (note["when_text"] or "").strip()
    return f"{text} (когда-то: {when})" if when else text


def _move_notes(bind) -> None:
    notes = bind.execute(sa.text(
        "SELECT id, user_id, text, kind, source, when_text, remind_at, reminded_at, created_at "
        "FROM notes ORDER BY id")).mappings().all()

    sections: dict = {}
    boards: dict = {}
    freshest: dict = {}
    for note in notes:
        if not (note["text"] or "").strip():
            continue
        if note["kind"] == "task" and note["remind_at"] is not None:
            _move_reminder(bind, note)
            continue

        kind = note["kind"] if note["kind"] in BOARDS else FALLBACK_KIND
        name, instruction = BOARDS[kind]
        user_id, at = note["user_id"], note["created_at"]
        if user_id not in sections:
            sections[user_id] = _section_id(bind, user_id, at)
        section_id = sections[user_id]
        if (section_id, name) not in boards:
            boards[(section_id, name)] = _board_id(bind, section_id, name, instruction, at)
        board_id = boards[(section_id, name)]

        _move_entry(bind, note, board_id)
        # Свежесть доски и раздела — это время последней записи на них: по ней
        # сортируются и полоса разделов, и список досок.
        freshest[board_id] = max(at, freshest.get(board_id, at))
        freshest[("section", section_id)] = max(at, freshest.get(("section", section_id), at))

    # Только вперёд: раздел «Личное» мог уже жить со своей «Памятью ассистента»,
    # и переезд старых заметок не должен утаскивать его вниз по свежести.
    for key, at in freshest.items():
        table, row_id = ("sections", key[1]) if isinstance(key, tuple) else ("boards", key)
        bind.execute(sa.text(f"UPDATE {table} SET last_activity_at = :at "
                             f"WHERE id = :id AND last_activity_at < :at"),
                     {"at": at, "id": row_id})


def _move_entry(bind, note, board_id: int) -> None:
    by_assistant = note["source"] != BY_HAND
    bind.execute(sa.text(
        "INSERT INTO board_entries (board_id, author_id, by_assistant, text, created_at, edited_at) "
        "VALUES (:board_id, :author_id, :by_assistant, :text, :created_at, NULL)"),
        {"board_id": board_id,
         "author_id": None if by_assistant else note["user_id"],
         "by_assistant": by_assistant,
         "text": _entry_text(note),
         "created_at": note["created_at"]})


def _move_reminder(bind, note) -> None:
    """Заметка со сроком — в свою таблицу, не теряя того, что уже сработало."""
    bind.execute(sa.text(
        "INSERT INTO reminders (user_id, text, remind_at, reminded_at, created_at) "
        "VALUES (:user_id, :text, :remind_at, :reminded_at, :created_at)"),
        {"user_id": note["user_id"], "text": (note["text"] or "").strip(),
         "remind_at": note["remind_at"], "reminded_at": note["reminded_at"],
         "created_at": note["created_at"]})
