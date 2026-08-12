"""Сервис знаний: разделы → доски → записи (спека #19).

Пока здесь живут разделы (#25) и доски (#26). Единая точка разрешения доступа
к доскам — «какие доски видит этот участник и с каким правом» — появится
вместе с шарингом (#28); до него доска видна только владельцу её раздела,
и ходить к доскам мимо этих функций нельзя уже сейчас.

Раздел строго личный: каждая функция принимает `user_id` смотрящего и не
отдаёт и не трогает чужого. Проверка владельца здесь, а не в маршрутах,
чтобы у экрана и будущих инструментов агента была одна и та же граница.
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.modules.memory.models import Board, BoardEntry, Section


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


# --- доски (#26) --------------------------------------------------------------

def list_boards(db: Session, user_id: int, section_id: int) -> List[Board]:
    """Доски раздела по свежести. Чужой раздел отдаёт пустоту, а не ошибку."""
    if get_section(db, user_id, section_id) is None:
        return []
    return (
        db.query(Board)
        .filter(Board.section_id == section_id)
        .order_by(Board.last_activity_at.desc(), Board.id.desc())
        .all()
    )


def get_board(db: Session, user_id: int, board_id: int) -> Optional[Board]:
    board = db.get(Board, board_id)
    if board is None or get_section(db, user_id, board.section_id) is None:
        return None
    return board


def create_board(db: Session, user_id: int, section_id: int, name: str,
                 instruction: Optional[str] = None) -> Optional[Board]:
    section = get_section(db, user_id, section_id)
    name = name.strip()[:NAME_LIMIT]
    if section is None or not name:
        return None
    board = Board(section_id=section.id, name=name,
                  instruction=(instruction or "").strip() or None)
    # Новая доска — активность раздела: полоса разделов сортируется по ней.
    section.last_activity_at = datetime.utcnow()
    db.add(board)
    db.commit()
    db.refresh(board)
    return board


def update_board(db: Session, user_id: int, board_id: int, name: str,
                 instruction: Optional[str] = None,
                 section_id: Optional[int] = None) -> Optional[Board]:
    """Имя, инструкция ассистенту и, если передан `section_id`, перенос в другой
    свой раздел — одной транзакцией: невалидный перенос отменяет и правку,
    частичного сохранения не бывает. Инструкцию правит только владелец доски —
    до шаринга (#28) сюда и не попасть никому, кроме него."""
    board = get_board(db, user_id, board_id)
    name = name.strip()[:NAME_LIMIT]
    if board is None or not name:
        return None
    if section_id is not None and section_id != board.section_id:
        target = get_section(db, user_id, section_id)
        if target is None:
            return None
        _relocate(db, board, target)
    board.name = name
    board.instruction = (instruction or "").strip() or None
    db.commit()
    return board


def move_board(db: Session, user_id: int, board_id: int,
               target_section_id: int) -> Optional[Board]:
    """Перенос доски между своими разделами: перекладка тем без переписывания записей."""
    board = get_board(db, user_id, board_id)
    target = get_section(db, user_id, target_section_id)
    if board is None or target is None:
        return None
    if target.id != board.section_id:
        _relocate(db, board, target)
    db.commit()
    return board


def _relocate(db: Session, board: Board, target: Section) -> None:
    """Доска уносит свою активность с собой: денормализованное время раздела
    честно пересчитывается и у источника, и у цели, иначе полоса разделов
    сортировалась бы по уехавшей доске. Коммитит вызывающий."""
    source_id = board.section_id
    board.section_id = target.id
    target.last_activity_at = max(target.last_activity_at, board.last_activity_at)
    rest = [b.last_activity_at for b in db.query(Board)
            .filter(Board.section_id == source_id, Board.id != board.id)]
    source = db.get(Section, source_id)
    source.last_activity_at = max(rest) if rest else source.created_at


def delete_board(db: Session, user_id: int, board_id: int) -> bool:
    """Каскад доска → записи; блокировка при активном доступе появится в #28."""
    board = get_board(db, user_id, board_id)
    if board is None:
        return False
    db.delete(board)
    db.commit()
    return True


# --- записи (#27) ---------------------------------------------------------------

#: Лента отдаёт хвост лога: у семьи это годы записей, экрану нужны последние.
FEED_LIMIT = 500


def list_entries(db: Session, user_id: int, board_id: int) -> List[BoardEntry]:
    """Лента доски по времени, как разговор: старые сверху, свежие внизу."""
    if get_board(db, user_id, board_id) is None:
        return []
    tail = (
        db.query(BoardEntry)
        .filter(BoardEntry.board_id == board_id)
        .order_by(BoardEntry.created_at.desc(), BoardEntry.id.desc())
        .limit(FEED_LIMIT)
        .all()
    )
    return list(reversed(tail))


def add_entry(db: Session, user_id: int, board_id: int, text: str) -> Optional[BoardEntry]:
    board = get_board(db, user_id, board_id)
    text = text.strip()
    if board is None or not text:
        return None
    entry = BoardEntry(board_id=board.id, author_id=user_id, text=text)
    # Запись — и есть активность: по ней сортируются и доски, и полоса разделов.
    now = datetime.utcnow()
    board.last_activity_at = now
    db.get(Section, board.section_id).last_activity_at = now
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_entry(db: Session, user_id: int, entry_id: int) -> Optional[BoardEntry]:
    """Запись, которую этот человек вправе править или удалить.

    Пока доски видит только владелец, право сводится к «запись на моей доске»:
    владелец правит любые записи своей доски. С шарингом (#28) сюда добавится
    второй путь — автор записи на доступной ему доске.
    """
    entry = db.get(BoardEntry, entry_id)
    if entry is None or get_board(db, user_id, entry.board_id) is None:
        return None
    return entry


def edit_entry(db: Session, user_id: int, entry_id: int, text: str) -> Optional[BoardEntry]:
    """Правка не тихая: у поправленной записи в ленте видна пометка «изменено»."""
    entry = get_entry(db, user_id, entry_id)
    text = text.strip()
    if entry is None or not text:
        return None
    entry.text = text
    entry.edited_at = datetime.utcnow()
    db.commit()
    return entry


def delete_entry(db: Session, user_id: int, entry_id: int) -> bool:
    entry = get_entry(db, user_id, entry_id)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    return True
