"""Сервис знаний: разделы → доски → записи → доступ (спека #19).

Весь инвариант приватности сводится к одной точке — `board_grants`: «какие
доски видит этот участник и с каким правом». Через неё обязаны ходить и
экраны, и инструменты агента; второго пути к доскам в коде нет. Прецедент
принципа — `can_see_figures` в app/core/auth.py.

Раздел строго личный: каждая функция принимает `user_id` смотрящего и не
отдаёт и не трогает чужого. Проверка прав здесь, а не в маршрутах, чтобы
у экрана и инструментов агента была одна и та же граница.
"""
from datetime import datetime
from typing import List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.core.models import User
from app.modules.memory import extraction
from app.modules.memory.models import (
    Board, BoardEntry, BoardEvent, BoardEventType, BoardShare, Section, RIGHT_EDIT, RIGHT_VIEW,
)

#: Владелец — не третье право из board_shares, а вычисленное «доска в моём
#: разделе»: только он правит инструкцию, чужие записи и состав доступа.
RIGHT_OWNER = "owner"

RIGHT_LABELS = {RIGHT_VIEW: "просмотр", RIGHT_EDIT: "редактирование"}


class ActiveShares(Exception):
    """Удаление блокируется: есть доска с активным доступом.

    Отзыв доступа должен быть осознанным шагом, а не побочным эффектом сноса.
    """


class BoardGrant(NamedTuple):
    board: Board
    right: str   # RIGHT_OWNER / RIGHT_EDIT / RIGHT_VIEW


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

    Раздел с расшаренной доской не удаляется (ActiveShares): нельзя снести
    людям то, чем они пользуются, побочным эффектом.
    """
    section = get_section(db, user_id, section_id)
    if section is None:
        return False
    boards = db.query(Board).filter(Board.section_id == section.id).all()
    if any(_has_active_access(db, board) for board in boards):
        raise ActiveShares()
    db.delete(section)
    db.commit()
    return True


# --- разрешение доступа: единственная точка (#28) -------------------------------

def board_grants(db: Session, user_id: int) -> List[BoardGrant]:
    """Какие доски видит этот участник и с каким правом.

    Три источника: свои (через раздел), расшаренные поимённо (board_shares)
    и расшаренные «всем» в семье владельца — живое условие, а не снимок.
    Когда поимённое право и «всем» расходятся, действует более широкое.
    """
    me = db.get(User, user_id)
    if me is None:
        return []
    grants: dict = {}
    own = (db.query(Board)
           .join(Section, Board.section_id == Section.id)
           .filter(Section.user_id == user_id))
    for board in own:
        grants[board.id] = BoardGrant(board, RIGHT_OWNER)
    named = (db.query(Board, BoardShare.right)
             .join(BoardShare, BoardShare.board_id == Board.id)
             .filter(BoardShare.user_id == user_id))
    for board, right in named:
        if board.id not in grants:
            grants[board.id] = BoardGrant(board, right)
    family_wide = (db.query(Board)
                   .join(Section, Board.section_id == Section.id)
                   .join(User, Section.user_id == User.id)
                   .filter(Board.share_all.is_(True),
                           User.family_id == me.family_id,
                           Section.user_id != user_id))
    for board in family_wide:
        current = grants.get(board.id)
        wider = current is None or (current.right == RIGHT_VIEW
                                    and board.share_all_right == RIGHT_EDIT)
        if wider:
            grants[board.id] = BoardGrant(board, board.share_all_right or RIGHT_VIEW)
    return list(grants.values())


def board_access(db: Session, user_id: int, board_id: int) -> Optional[BoardGrant]:
    """Право этого участника на одну доску — из той же единственной точки."""
    return next((g for g in board_grants(db, user_id) if g.board.id == board_id), None)


def shared_boards(db: Session, user_id: int) -> List[BoardGrant]:
    """«Общее»: расшаренные на меня доски по свежести. Строки в базе у него нет —
    это отображение грантов."""
    shared = [g for g in board_grants(db, user_id) if g.right != RIGHT_OWNER]
    return sorted(shared, key=lambda g: (g.board.last_activity_at, g.board.id), reverse=True)


def board_owner_names(db: Session, grants: List[BoardGrant]) -> dict:
    """Имена владельцев для списка грантов — одним запросом, не по доске за раз."""
    section_ids = {g.board.section_id for g in grants}
    if not section_ids:
        return {}
    rows = (db.query(Section.id, User.display_name)
            .join(User, Section.user_id == User.id)
            .filter(Section.id.in_(section_ids)))
    names = dict(rows)
    return {g.board.id: names.get(g.board.section_id, "бывший участник") for g in grants}


# --- доски (#26) --------------------------------------------------------------

def list_boards(db: Session, user_id: int, section_id: int) -> List[Board]:
    """Доски раздела по свежести. Чужой раздел отдаёт пустоту, а не ошибку."""
    if get_section(db, user_id, section_id) is None:
        return []
    boards = [g.board for g in board_grants(db, user_id)
              if g.board.section_id == section_id]
    return sorted(boards, key=lambda b: (b.last_activity_at, b.id), reverse=True)


def get_board(db: Session, user_id: int, board_id: int) -> Optional[Board]:
    """Доска, которую этому участнику можно хотя бы читать."""
    grant = board_access(db, user_id, board_id)
    return grant.board if grant is not None else None


def _own_board(db: Session, user_id: int, board_id: int) -> Optional[Board]:
    """Доска, которой этот участник владеет: инструкция, перенос, доступ, снос."""
    grant = board_access(db, user_id, board_id)
    return grant.board if grant is not None and grant.right == RIGHT_OWNER else None


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
    частичного сохранения не бывает. Всё это — только владельцу доски:
    инструкция меняет поведение ассистента для всех допущенных."""
    board = _own_board(db, user_id, board_id)
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
    board = _own_board(db, user_id, board_id)
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


def _has_active_access(db: Session, board: Board) -> bool:
    return bool(board.share_all) or bool(
        db.query(BoardShare.id).filter(BoardShare.board_id == board.id).first())


def delete_board(db: Session, user_id: int, board_id: int) -> bool:
    """Каскад доска → записи. Доска с активным доступом не удаляется:
    сначала осознанный отзыв, потом снос (ActiveShares)."""
    board = _own_board(db, user_id, board_id)
    if board is None:
        return False
    if _has_active_access(db, board):
        raise ActiveShares()
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


def add_entry(db: Session, user_id: int, board_id: int, text: str,
              llm=None) -> Optional[BoardEntry]:
    grant = board_access(db, user_id, board_id)
    text = text.strip()
    if grant is None or grant.right == RIGHT_VIEW or not text:
        return None
    board = grant.board
    if board.name == RULES_BOARD_NAME:
        # Реестр правил — обычная доска (ADR-0011), и обычный ввод на неё идёт
        # тем же путём, что и любая другая запись. Но лимит длины/числа у
        # `add_rule` существует не как защита, а как гарантия для промпта
        # (RULE_LIMIT/RULES_MAX выше): без него запись, пришедшая не через
        # set_rule, а прямо с этой доски, ехала бы в каждый разговор без
        # обрезки и без счёта. Та же гарантия здесь, что и там.
        text = _trim_rule_text(text)
        if len(list_rules(db, user_id)) >= RULES_MAX:
            return None
    entry = BoardEntry(board_id=board.id, author_id=user_id, text=text)
    # Запись — и есть активность: по ней сортируются и доски, и полоса разделов.
    now = datetime.utcnow()
    board.last_activity_at = now
    db.get(Section, board.section_id).last_activity_at = now
    db.add(entry)
    db.commit()
    db.refresh(entry)
    parse_entry(db, entry, llm=llm)
    return entry


def get_entry(db: Session, user_id: int, entry_id: int) -> Optional[BoardEntry]:
    """Запись, которую этот человек вправе править или удалить.

    Два пути: владелец доски — любые записи своей доски; допущенный на
    редактирование — только свои. Допущенному на просмотр запись не отдаётся
    вовсе, даже собственная давняя: его полномочия — читать и копировать.
    """
    entry = db.get(BoardEntry, entry_id)
    if entry is None:
        return None
    grant = board_access(db, user_id, entry.board_id)
    if grant is None:
        return None
    if grant.right == RIGHT_OWNER:
        return entry
    if grant.right == RIGHT_EDIT and entry.author_id == user_id:
        return entry
    return None


def edit_entry(db: Session, user_id: int, entry_id: int, text: str,
               llm=None) -> Optional[BoardEntry]:
    """Правка не тихая: у поправленной записи в ленте видна пометка «изменено».

    Правка переразбирает величины записи: исправленная опечатка в цифре обязана
    попасть в статистику, а старый разбор — уйти вместе с прежним текстом.
    """
    entry = get_entry(db, user_id, entry_id)
    text = text.strip()
    if entry is None or not text:
        return None
    board = db.get(Board, entry.board_id)
    if board is not None and board.name == RULES_BOARD_NAME:
        text = _trim_rule_text(text)
    entry.text = text
    entry.edited_at = datetime.utcnow()
    db.commit()
    parse_entry(db, entry, llm=llm)
    return entry


def delete_entry(db: Session, user_id: int, entry_id: int) -> bool:
    entry = get_entry(db, user_id, entry_id)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    return True


# --- события: словарь типов и разбор записи (#30) --------------------------------

#: Сколько вариантов показывать в плашке уточнения. Больше — уже не тихий
#: вопрос под записью, а анкета.
CLARIFY_OPTIONS = 3

TYPE_NAME_LIMIT = 64
UNIT_LIMIT = 16


def list_event_types(db: Session, board_id: int) -> List[BoardEventType]:
    """Словарь величин доски в порядке заведения — им и разбирают её записи."""
    return (db.query(BoardEventType)
            .filter(BoardEventType.board_id == board_id)
            .order_by(BoardEventType.id)
            .all())


def add_event_type(db: Session, user_id: int, board_id: int, name: str,
                   unit: str = None) -> Optional[BoardEventType]:
    """Завести тип величины на доске.

    Заводить его вправе всякий, кто ведёт лог (не только владелец): у
    допущенного на редактирование иначе не было бы чем ответить на плашку
    уточнения под собственной записью. Повторное имя ничего не дублирует.
    """
    grant = board_access(db, user_id, board_id)
    name = (name or "").strip()[:TYPE_NAME_LIMIT]
    if grant is None or grant.right == RIGHT_VIEW or not name:
        return None
    existing = next((t for t in list_event_types(db, board_id)
                     if t.name.lower() == name.lower()), None)
    if existing is not None:
        return existing
    event_type = BoardEventType(board_id=board_id, name=name,
                                unit=(unit or "").strip()[:UNIT_LIMIT] or None)
    db.add(event_type)
    db.commit()
    db.refresh(event_type)
    return event_type


def parse_entry(db: Session, entry: BoardEntry, llm=None) -> List[BoardEvent]:
    """Превратить запись в величины — при её создании и при каждой правке.

    Единственная точка разбора: и панель, и `write_entry` ассистента приходят
    сюда через add_entry/edit_entry, поэтому величины не зависят от пути записи.

    Доска без словаря типов не разбирается вовсе — модель для неё не зовут:
    тип берётся из словаря, а разбирать не во что.

    Прежние величины уходят только вместе с состоявшимся разбором: если модель
    не ответила, они остаются на месте — уточнённое человеком не стирается
    оттого, что модель моргнула. Сама запись к этому моменту уже сохранена, так
    что человек ждёт здесь только цифру, а не сохранность своих слов.
    """
    board = db.get(Board, entry.board_id)
    types = list_event_types(db, entry.board_id)
    if board is None or not types:
        return entry_events(db, entry.id)

    extracted = extraction.safe_extract_events(
        entry.text, instruction=board.instruction, types=types, at=entry.created_at, llm=llm)
    if extracted is None:
        return entry_events(db, entry.id)

    db.query(BoardEvent).filter(BoardEvent.entry_id == entry.id).delete()
    events = [BoardEvent(entry_id=entry.id, board_id=entry.board_id, kind=e.kind, at=e.at,
                         value=e.value, unit=e.unit, confidence=e.confidence, raw=e.raw)
              for e in extracted]
    db.add_all(events)
    db.commit()
    return events


def entry_events(db: Session, entry_id: int) -> List[BoardEvent]:
    return (db.query(BoardEvent)
            .filter(BoardEvent.entry_id == entry_id)
            .order_by(BoardEvent.at, BoardEvent.id)
            .all())


def board_events(db: Session, board_id: int, since: datetime = None,
                 until: datetime = None) -> List[BoardEvent]:
    rows = db.query(BoardEvent).filter(BoardEvent.board_id == board_id)
    if since is not None:
        rows = rows.filter(BoardEvent.at >= since)
    if until is not None:
        rows = rows.filter(BoardEvent.at < until)
    return rows.order_by(BoardEvent.at, BoardEvent.id).all()


def event_totals(db: Session, user_id: int, board_id: int, since: datetime = None,
                 until: datetime = None) -> List[dict]:
    """Суммы по типам за период — то, из чего потом растут сводка и табло.

    Считает код, а не модель. Неуверенно разобранное в сумму не идёт, пока
    человек не уточнил: цифра, которой нельзя верить, хуже отсутствующей.

    Сумма идёт по типу вместе с единицей: миллилитры и литры одного типа — две
    строки, а не одно число, которое неизвестно в чём.
    """
    if get_board(db, user_id, board_id) is None:
        return []
    totals: dict = {}
    for event in board_events(db, board_id, since, until):
        if event.confidence == extraction.LOW:
            continue
        row = totals.setdefault((event.kind, event.unit),
                                {"kind": event.kind, "unit": event.unit,
                                 "total": 0.0, "count": 0})
        row["total"] += event.value
        row["count"] += 1
    return [totals[key] for key in sorted(totals, key=lambda key: (key[0], key[1] or ""))]


def clarifications(db: Session, board_id: int, entry_ids: Sequence[int]) -> dict:
    """Что ждёт уточнения под показанными записями: величина → чем на неё ответить.

    Варианты — типы словаря доски, разобранный первым: человек отвечает одним
    нажатием, а свои слова пишет в поле рядом. Спрашивают только про записи,
    которые сейчас в ленте: за её хвостом плашке всё равно негде показаться.
    """
    names = [t.name for t in list_event_types(db, board_id)]
    if not names or not entry_ids:
        return {}
    waiting: dict = {}
    uncertain = (db.query(BoardEvent)
                 .filter(BoardEvent.entry_id.in_(list(entry_ids)),
                         BoardEvent.confidence == extraction.LOW)
                 .order_by(BoardEvent.at, BoardEvent.id))
    for event in uncertain:
        guessed = [n for n in names if n.lower() == event.kind.lower()]
        options = guessed + [n for n in names if n not in guessed]
        waiting.setdefault(event.entry_id, []).append({
            "id": event.id,
            "raw": event.raw or "",
            "options": options[:CLARIFY_OPTIONS],
        })
    return waiting


def event_board(db: Session, user_id: int, event_id: int) -> Optional[int]:
    """Доска величины глазами смотрящего — чтобы вернуть его на неё после ответа."""
    event = db.get(BoardEvent, event_id)
    if event is None or get_board(db, user_id, event.board_id) is None:
        return None
    return event.board_id


def clarify_event(db: Session, user_id: int, event_id: int, kind: str) -> Optional[BoardEvent]:
    """Ответ человека на плашку: величина получает тип и идёт в счёт.

    Отвечает тот, кто вправе править саму запись, — автор или владелец доски.
    Названного типа в словаре нет — он там заводится: человек назвал величину
    своими словами, и словарь обязан догнать его, а не наоборот.
    """
    event = db.get(BoardEvent, event_id)
    kind = (kind or "").strip()[:TYPE_NAME_LIMIT]
    if event is None or not kind:
        return None
    if get_entry(db, user_id, event.entry_id) is None:
        return None

    event_type = add_event_type(db, user_id, event.board_id, kind, unit=event.unit)
    if event_type is None:
        return None
    event.kind = event_type.name
    event.unit = event.unit or event_type.unit
    # Человек сказал сам — уверенности выше не бывает, величина идёт в сумму.
    event.confidence = extraction.HIGH
    db.commit()
    return event


# --- доступ: шаринг и отзыв (#28) -----------------------------------------------

def share_board(db: Session, user_id: int, board_id: int, member_id: int,
                right: str) -> bool:
    """Поделиться доской с конкретным участником семьи — на просмотр или
    редактирование. Повторный шаринг тому же человеку меняет право."""
    board = _own_board(db, user_id, board_id)
    if board is None or right not in RIGHT_LABELS or member_id == user_id:
        return False
    owner = db.get(User, user_id)
    target = db.get(User, member_id)
    if target is None or target.family_id != owner.family_id:
        return False
    existing = (db.query(BoardShare)
                .filter(BoardShare.board_id == board.id, BoardShare.user_id == member_id)
                .one_or_none())
    if existing is not None:
        existing.right = right
    else:
        db.add(BoardShare(board_id=board.id, user_id=member_id, right=right))
    db.commit()
    return True


def share_board_with_all(db: Session, user_id: int, board_id: int, right: str) -> bool:
    """«Всем» — живое условие на доске: новый человек в семье получит её сам."""
    board = _own_board(db, user_id, board_id)
    if board is None or right not in RIGHT_LABELS:
        return False
    board.share_all = True
    board.share_all_right = right
    db.commit()
    return True


def stop_sharing_with_all(db: Session, user_id: int, board_id: int) -> bool:
    board = _own_board(db, user_id, board_id)
    if board is None:
        return False
    board.share_all = False
    board.share_all_right = None
    db.commit()
    return True


def revoke_share(db: Session, user_id: int, board_id: int, member_id: int) -> bool:
    """Отзыв тихий: доска просто исчезает из «Общего», записи остаются на ней —
    запись принадлежит документу, а не автору."""
    board = _own_board(db, user_id, board_id)
    if board is None:
        return False
    (db.query(BoardShare)
     .filter(BoardShare.board_id == board.id, BoardShare.user_id == member_id)
     .delete())
    db.commit()
    return True


# --- инструменты агента (#29) ----------------------------------------------------

#: Личная доска ассистента: сюда он складывает то, что запомнил из разговора.
ASSISTANT_BOARD_NAME = "Память ассистента"
ASSISTANT_SECTION_NAME = "Личное"
ASSISTANT_BOARD_INSTRUCTION = ("Сюда ассистент складывает то, что запомнил из "
                               "разговоров: предпочтения, ограничения, наблюдения. "
                               "Проверяй здесь перед советами о еде, планах и покупках "
                               "и когда человек спрашивает «что ты помнишь».")

#: Реестр правил — вторая доска, которую ассистент заводит себе сам. Одна запись
#: здесь равна одному правилу, и все они доезжают до модели в каждом разговоре.
RULES_BOARD_NAME = "Поведение помощника"
RULES_BOARD_INSTRUCTION = ("Реестр правил: что человек однажды велел делать всегда. "
                           "Одна запись — одно правило; ассистент читает их все "
                           "в каждом разговоре.")

#: Знаков в одном правиле и правил у человека. Ограничение не про безопасность,
#: а про окно контекста: правила едут в каждый запрос — как `CHARACTER_LIMIT`
#: у характера, только счётом ещё и по строкам.
RULE_LIMIT = 240
RULES_MAX = 20


class TooManyRules(Exception):
    """Правил столько, что реестр перестал быть обозримым.

    Отказ, а не тихое вытеснение старого: правило, которое ассистент забыл сам,
    человек считает действующим, и разошлись бы они молча.
    """


def _trim_rule_text(text: str) -> str:
    """Обрезка правила по RULE_LIMIT — с явным «…», а не тихим обрывом на
    полуслове. Подтверждающая карточка после обрезки читалась бы законченной
    мыслью, которой на самом деле не было, — человек не заметил бы потери."""
    text = (text or "").strip()
    if len(text) <= RULE_LIMIT:
        return text
    return text[:RULE_LIMIT - 1].rstrip() + "…"


def find_boards_by_name(db: Session, user_id: int, name: str) -> List[BoardGrant]:
    """Доска по имени среди доступных: точное совпадение без регистра, иначе
    вхождение. Несколько совпадений — повод переспросить, а не угадывать."""
    grants = board_grants(db, user_id)
    lowered = name.strip().lower()
    if not lowered:
        return []
    exact = [g for g in grants if g.board.name.lower() == lowered]
    if exact:
        return exact
    return [g for g in grants if lowered in g.board.name.lower()]


def find_sections_by_name(db: Session, user_id: int, name: str) -> List[Section]:
    lowered = name.strip().lower()
    if not lowered:
        return []
    sections = list_sections(db, user_id)
    exact = [s for s in sections if s.name.lower() == lowered]
    if exact:
        return exact
    return [s for s in sections if lowered in s.name.lower()]


def search_entries(db: Session, user_id: int, query: str, limit: int = 8):
    """Поиск подстрокой по всем доступным доскам; выдача помнит, с какой доски
    факт, — иначе он теряет контекст. Поиск не дотягивается до чужого:
    границу держит board_grants. Пустой запрос — просто свежие записи:
    «что ты помнишь» без ключевого слова тоже законный вопрос."""
    boards = {g.board.id: g.board for g in board_grants(db, user_id)}
    if not boards:
        return []
    rows = db.query(BoardEntry).filter(BoardEntry.board_id.in_(boards))
    needle = (query or "").strip()
    if needle:
        # % и _ в запросе — буквы, а не метасимволы LIKE: «100%» не должен
        # находить «1000 шагов».
        escaped = (needle.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_"))
        rows = rows.filter(BoardEntry.text.ilike(f"%{escaped}%", escape="\\"))
    rows = (rows.order_by(BoardEntry.created_at.desc(), BoardEntry.id.desc())
            .limit(limit)
            .all())
    return [(entry, boards[entry.board_id]) for entry in rows]


def person_facts(db: Session, user_id: int, limit: int = 8) -> List[str]:
    """Свежее с досок, где записывают факты, а не измеряют, — строками.

    Доска со словарём величин — это лог: кормления, шаги, показания счётчиков.
    Его последние записи не рассказывают о человеке ничего («02:50 170»), зато
    вытеснили бы из короткой выжимки то единственное, ради чего её собирают, —
    «у Лёвы аллергия на арахис». Поэтому доски, которые ведут счёт, сюда не идут.

    Реестра правил тут нет по той же причине, только с другой стороны: правило
    рассказывает не о человеке, а об ассистенте, и в подборе блюд ему делать
    нечего — там ждут аллергию, а не «записывай состояние на доску».
    """
    boards = {g.board.id for g in board_grants(db, user_id)
              if g.board.name != RULES_BOARD_NAME}
    if not boards:
        return []
    counted = {row[0] for row in db.query(BoardEventType.board_id)
               .filter(BoardEventType.board_id.in_(boards)).distinct()}
    boards -= counted
    if not boards:
        return []
    rows = (db.query(BoardEntry)
            .filter(BoardEntry.board_id.in_(boards))
            .order_by(BoardEntry.created_at.desc(), BoardEntry.id.desc())
            .limit(limit)
            .all())
    return [entry.text for entry in rows]


def _system_board(db: Session, user_id: int, name: str, instruction: str,
                  create: bool = True) -> Optional[Board]:
    """Доска, которую ассистент завёл себе сам, — вместе с разделом «Личное».

    Заводится лениво и только тогда, когда в неё правда пишут: `create=False`
    отдаёт то, что уже есть. Разница не косметическая — правила читаются на
    каждом ходу, и чтение не должно заводить пустой реестр тому, кто ни о чём
    не договаривался.
    """
    board = (db.query(Board)
             .join(Section, Board.section_id == Section.id)
             .filter(Section.user_id == user_id, Board.name == name)
             .first())
    if board is not None or not create:
        return board
    section = (db.query(Section)
               .filter(Section.user_id == user_id, Section.name == ASSISTANT_SECTION_NAME)
               .first())
    if section is None:
        section = Section(user_id=user_id, name=ASSISTANT_SECTION_NAME)
        db.add(section)
        db.flush()
    board = Board(section_id=section.id, name=name, instruction=instruction)
    db.add(board)
    db.commit()
    db.refresh(board)
    return board


def assistant_board(db: Session, user_id: int) -> Board:
    """Доска «Память ассистента» — заводится лениво при первом запоминании."""
    return _system_board(db, user_id, ASSISTANT_BOARD_NAME, ASSISTANT_BOARD_INSTRUCTION)


def rules_board(db: Session, user_id: int, create: bool = False) -> Optional[Board]:
    """Реестр правил — заводится лениво при первом уговоре, а не при чтении."""
    return _system_board(db, user_id, RULES_BOARD_NAME, RULES_BOARD_INSTRUCTION,
                         create=create)


def add_assistant_entry(db: Session, user_id: int, board_id: int, text: str,
                        llm=None) -> Optional[BoardEntry]:
    """Запись авторства ассистента (author_id NULL, by_assistant) — только на
    доску, которой владеет человек: по своей инициативе ассистент на
    расшаренные доски не пишет."""
    board = _own_board(db, user_id, board_id)
    text = text.strip()
    if board is None or not text:
        return None
    entry = BoardEntry(board_id=board.id, author_id=None, by_assistant=True, text=text)
    now = datetime.utcnow()
    board.last_activity_at = now
    db.get(Section, board.section_id).last_activity_at = now
    db.add(entry)
    db.commit()
    db.refresh(entry)
    parse_entry(db, entry, llm=llm)
    return entry


def delete_assistant_entry(db: Session, user_id: int, entry_id: int) -> bool:
    """Ассистент удаляет только свои записи: необратимое действие в общем
    документе остаётся за человеком."""
    entry = db.get(BoardEntry, entry_id)
    if entry is None or not entry.by_assistant:
        return False
    if _own_board(db, user_id, entry.board_id) is None:
        return False
    db.delete(entry)
    db.commit()
    return True


# --- правила ---------------------------------------------------------------------

def list_rules(db: Session, user_id: int) -> List[BoardEntry]:
    """Действующие правила человека — записями реестра, старые впереди.

    Ни о чём не договаривались — пустой список и ни одной заведённой доски.
    """
    board = rules_board(db, user_id)
    return list_entries(db, user_id, board.id) if board is not None else []


def add_rule(db: Session, user_id: int, text: str,
             replaces: int = None) -> Optional[BoardEntry]:
    """Записать правило в реестр, при надобности сняв замещаемое.

    Замена — одним действием, а не «завести и потом забыть снять»: два правила
    об одном и том же противоречат друг другу молча, и разбирать это придётся
    модели посреди разговора.
    """
    text = _trim_rule_text(text)
    if not text:
        return None

    superseded = replaces is not None and drop_rule(db, user_id, replaces)
    if len(list_rules(db, user_id)) >= RULES_MAX and not superseded:
        raise TooManyRules()

    board = rules_board(db, user_id, create=True)
    return add_assistant_entry(db, user_id, board.id, text)


def drop_rule(db: Session, user_id: int, entry_id: int) -> bool:
    """Снять правило. Только из реестра: запись с любой другой доски — не правило,
    и удалять её этим путём нельзя, даже если её тоже написал ассистент."""
    board = rules_board(db, user_id)
    entry = db.get(BoardEntry, entry_id)
    if board is None or entry is None or entry.board_id != board.id:
        return False
    return delete_assistant_entry(db, user_id, entry_id)


def rules_for_prompt(db: Session, user_id: int) -> List[Tuple[int, str]]:
    """Правила для системного промпта — парами «номер, текст».

    Номер — это номер записи в реестре: им человек и ссылается на правило,
    когда просит его снять или поправить.
    """
    return [(entry.id, entry.text) for entry in list_rules(db, user_id)[:RULES_MAX]]


def rules_url(db: Session, user_id: int) -> str:
    """Адрес реестра для экрана профиля — пустая строка, если его ещё нет."""
    board = rules_board(db, user_id)
    if board is None:
        return ""
    return board_url(BoardGrant(board=board, right=RIGHT_OWNER))


def boards_prompt(db: Session, user_id: int) -> str:
    """Названия и инструкции доступных досок — для системного промпта.

    По одному названию модель не понимает, что доска значит; содержимое при
    этом в контекст не кладётся — только инструментом read_board (спека #19).

    Реестра правил в этом перечне нет: его содержимое и так едет в промпт
    целиком, а строка в списке досок звала бы модель писать туда `write_entry` —
    мимо `set_rule` и мимо подтверждения человеком.
    """
    grants = [g for g in board_grants(db, user_id) if g.board.name != RULES_BOARD_NAME]
    if not grants:
        return ""
    owners = board_owner_names(db, grants)
    lines = []
    for grant in sorted(grants, key=lambda g: (g.board.last_activity_at, g.board.id),
                        reverse=True):
        where = ("своя" if grant.right == RIGHT_OWNER
                 else f"{owners[grant.board.id]}, {RIGHT_LABELS[grant.right]}")
        instruction = f" — {grant.board.instruction}" if grant.board.instruction else ""
        lines.append(f"- «{grant.board.name}» ({where}){instruction}")
    return "Доски знаний, доступные этому человеку:\n" + "\n".join(lines) + "\n"


def board_url(grant: BoardGrant) -> str:
    """Адрес доски глазами получившего грант: своя — в своём разделе,
    расшаренная — в «Общем»."""
    if grant.right == RIGHT_OWNER:
        return f"/memory?section={grant.board.section_id}&board={grant.board.id}"
    return f"/memory?section=common&board={grant.board.id}"


def board_audience(db: Session, user_id: int, board_id: int) -> Optional[dict]:
    """Состав доступа к доске — его видит любой допущенный: понятно, кто
    прочтёт то, что ты пишешь."""
    grant = board_access(db, user_id, board_id)
    if grant is None:
        return None
    board = grant.board
    section = db.get(Section, board.section_id)
    shares = (db.query(User, BoardShare.right)
              .join(BoardShare, BoardShare.user_id == User.id)
              .filter(BoardShare.board_id == board.id)
              .order_by(User.display_name)
              .all())
    return {
        "owner": db.get(User, section.user_id),
        "shares": [(user, right) for user, right in shares],
        "all_right": board.share_all_right if board.share_all else None,
    }
