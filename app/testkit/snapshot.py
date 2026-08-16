"""Снимок мира одного человека — чтобы сценарий проверял данные, а не слова.

Ассистент отвечает словами, и по словам не видно, случилось ли что-нибудь на
самом деле: «записала» без записи выглядит ровно так же, как «записала» с
записью. Проверять надо строку в таблице.

Снимок собирается не по списку известных таблиц, а по тому, что лежит в
`Base.metadata`: модуль — это папка со своими таблицами, и ядро о них ничего не
знает (docs/module-contract.md). Появится четвёртый модуль — его таблицы
окажутся в снимке сами, без правки этого файла. Отбор идёт по колонкам: есть
`user_id` — берём строки этого человека, есть только `family_id` — строки его
семьи, нет ни того ни другого (доски, записи) — таблица общая для базы, а база
на стенде и так одна семья.

Значения приводятся к тому, что переживает JSON, и обрезаются: снимок читают
глазами и сравнивают в сценарии, а не хранят как выгрузку.
"""
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.models import User

#: Колонки, по которым таблица привязывается к человеку. Порядок — очерёдность
#: проверки: у прогона агента есть и `user_id`, и `subject_id`, и владелец у него
#: первый.
OWNER_COLUMNS = ("user_id", "author_id", "owner_id", "subject_id")

#: Строк одной таблицы по умолчанию. Сценарию нужен хвост — то, что он только что
#: наделал, — а не вся история.
DEFAULT_LIMIT = 20

#: Сколько знаков переживает значение поля.
VALUE_LIMIT = 600

#: Таблицы, которых в снимке по умолчанию нет. Шаги трейсов весят больше всего
#: остального вместе взятого, и у них своя ручка (`/api/testkit/traces`).
HEAVY = frozenset({"agent_trace_steps"})


def _value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} байт>"
    if isinstance(value, str) and len(value) > VALUE_LIMIT:
        return value[:VALUE_LIMIT] + f"… (всего {len(value)})"
    return value


def _scope(table) -> Optional[str]:
    for name in OWNER_COLUMNS:
        if name in table.columns:
            return name
    return "family_id" if "family_id" in table.columns else None


def tables() -> List[str]:
    return sorted(t.name for t in Base.metadata.sorted_tables)


def snapshot(db: Session, user: Optional[User], only: List[str] = None,
             limit: int = DEFAULT_LIMIT, include_heavy: bool = False) -> Dict[str, list]:
    """{таблица: [строки]} — хвост каждой таблицы глазами этого человека."""
    wanted = {name.strip() for name in (only or []) if name.strip()}
    result: Dict[str, list] = {}

    for table in Base.metadata.sorted_tables:
        if wanted and table.name not in wanted:
            continue
        if not wanted and table.name in HEAVY and not include_heavy:
            continue

        query = select(table)
        column = _scope(table)
        if user is not None and column:
            if column == "family_id":
                query = query.where(table.c.family_id == user.family_id)
            elif table.name == "users":
                # У самих людей `user_id` нет, а сузить до одного человека —
                # значит потерять из виду семью, которой он делится досками.
                query = query.where(table.c.family_id == user.family_id)
            else:
                query = query.where(table.c[column] == user.id)

        order = table.primary_key.columns.values()
        if order:
            query = query.order_by(order[0].desc())
        rows = db.execute(query.limit(limit)).mappings().all()
        result[table.name] = [{k: _value(v) for k, v in row.items()} for row in reversed(rows)]

    return result


def counts(db: Session, user: Optional[User]) -> Dict[str, int]:
    """Сколько строк в каждой таблице — тем же отбором, что и снимок.

    Дешёвая проверка «стало на одну запись больше»: сценарию часто нужна не
    строка, а разница до и после хода.
    """
    from sqlalchemy import func

    result: Dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        query = select(func.count()).select_from(table)
        column = _scope(table)
        if user is not None and column:
            if column == "family_id" or table.name == "users":
                query = query.where(table.c.family_id == user.family_id)
            else:
                query = query.where(table.c[column] == user.id)
        result[table.name] = int(db.execute(query).scalar() or 0)
    return result
