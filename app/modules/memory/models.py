"""Knowledge a person keeps with the assistant — personal, scoped by user_id.

Разделы → доски → записи плюс поимённый доступ (спека #19). Плоские заметки,
жившие здесь до них, переехали на доски миграцией 0008; их таблица осталась
рядом как `notes_legacy` — прочитать, а не работать.
"""
from datetime import datetime

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint)

from app.core.db import Base


# --- знания: разделы → доски → записи (спека #19) ---

#: Права доступа к чужой доске (см. board_shares.right).
RIGHT_VIEW = "view"    # просмотр
RIGHT_EDIT = "edit"    # редактирование: свои записи; чужие правит только владелец


class Section(Base):
    """Раздел — личная рубрика знаний. Чужих разделов не видит никто."""
    __tablename__ = "sections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(128), nullable=False)
    pinned = Column(Boolean, nullable=False, default=False)
    #: Денормализованное время последней записи на досках раздела — по нему
    #: сортируется полоса разделов (закреплённые, затем по свежести).
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Board(Base):
    """Доска — лента записей с инструкцией ассистенту.

    Владелец не дублируется: вычисляется через раздел (`section.user_id`), чтобы
    перенос доски между разделами не мог разъехаться с правами.
    """
    __tablename__ = "boards"

    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(128), nullable=False)
    #: Как ассистенту читать и вести содержимое: «19.50 170 — время и миллилитры».
    instruction = Column(Text, nullable=True)
    #: «Всем» — живое условие, а не снимок: новый человек в семье получает
    #: такую доску сам, без повторного действия владельца (спека #19).
    share_all = Column(Boolean, nullable=False, default=False)
    share_all_right = Column(String(8), nullable=True)
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardEntry(Base):
    """Запись в ленте доски. Принадлежит документу, а не автору (ADR-0004).

    Три вида авторства различаются парой полей, потому что `author_id = NULL`
    занят ушедшим участником: (id, false) — человек, (NULL, true) — ассистент,
    (NULL, false) — «бывший участник».
    """
    __tablename__ = "board_entries"

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    by_assistant = Column(Boolean, nullable=False, default=False)

    text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    #: Правки не тихие: у поправленной записи в ленте видна пометка «изменено».
    edited_at = Column(DateTime, nullable=True)


class BoardEventType(Base):
    """Словарь величин одной доски: «кормление» в мл, «прогулка» в минутах.

    Тип берётся отсюда, а не из головы модели: иначе «кормление», «еда» и
    «молоко» завелись бы на одной доске вперемешку. Съеденное и потраченное —
    два разных типа, а не число со знаком.
    """
    __tablename__ = "board_event_types"
    __table_args__ = (UniqueConstraint("board_id", "name", name="uq_board_event_type"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)

    name = Column(String(64), nullable=False)
    unit = Column(String(16), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardEvent(Base):
    """Величина, извлечённая из записи: тип, время, число и единица.

    Живёт ровно столько, сколько живёт её запись: правка записи переразбирает
    события, удаление — уносит. Разбор происходит один раз, при написании, а не
    при сборке сводки: цифра за прошлый вторник не должна меняться оттого, что
    сегодня модель прочла лог иначе (ADR-0002).
    """
    __tablename__ = "board_events"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("board_entries.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    #: Доска дублируется рядом с записью: статистику считают по доске за период,
    #: и ходить за этим в записи — лишний join на каждой сводке.
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String(64), nullable=False)
    at = Column(DateTime, nullable=False, index=True)
    value = Column(Float, nullable=False)
    unit = Column(String(16), nullable=True)
    #: low — в сумму не идёт, пока человек не уточнил (спека #19).
    confidence = Column(String(8), nullable=False, default="low")
    #: Фрагмент записи, из которого взята величина, — им и спрашивают человека.
    raw = Column(String(255), nullable=True)


class BoardStatsTask(Base):
    """Регулярная цифра по доске: что считать, в какую сводку и кому.

    Задачу ставит любой, кому доска доступна, — вопрос к общему логу не зависит
    от владельца. А вот рассылку результата всем допущенным включает только
    владелец: слать семье уведомления, ни с кем не согласовав, нельзя.

    Считает по задаче код (`kind` — тип из словаря доски), формулирует фразу
    модель по `request` — словам человека, которыми он задачу и поставил.
    """
    __tablename__ = "board_stats_tasks"

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    #: Слова человека: «каждое утро — сколько малыш съел за сутки».
    request = Column(Text, nullable=False)
    kind = Column(String(64), nullable=False)
    #: В какую из существующих сводок приходит результат: своего расписания у
    #: задачи нет — второй поток уведомлений семье не нужен.
    digest_kind = Column(String(32), nullable=False, default="morning_digest")
    share_all = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardStatsPoint(Base):
    """Снимок прогона: день и число, которое сводка в этот день назвала.

    Копится прогон за прогоном и живёт ровно столько, сколько живёт задача.
    Держит обещание сводки — одно число всем получателям одного дня, не
    переписываемое задним числом. Табло его не читает: свой ряд оно считает по
    самим событиям доски (ADR-0013).
    """
    __tablename__ = "board_stats_points"
    __table_args__ = (UniqueConstraint("task_id", "day", name="uq_board_stats_point"),)

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("board_stats_tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True)

    #: День прогона календарём семьи, а не гринвичским: ряд читают глазами
    #: человека. Это день, когда задача назвала число, а не непременно те сутки,
    #: за которые оно посчитано, — утренняя цифра «за сутки» захватывает ночь.
    day = Column(Date, nullable=False, index=True)
    value = Column(Float, nullable=False)
    unit = Column(String(16), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardStatsScreen(Base):
    """Табло — экран одного показателя по задаче статистики.

    Своего расписания у табло нет: ряд считает код по событиям доски при каждом
    показе (ADR-0013), поэтому экран живёт ровно столько, сколько живёт задача
    за ним (каскад), а та — сколько живёт её доска.

    Табло принадлежит смотрящему, а не задаче: по разосланному владельцем
    показателю каждый допущенный заводит своё — со своим названием и своим видом,
    потому что и пункт навигации у каждого свой.
    """
    __tablename__ = "board_stats_screens"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("board_stats_tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    #: Название, которое дал человек, — оно же подпись пункта навигации.
    name = Column(String(64), nullable=False)
    #: Одна из четырёх готовых форм (см. screens.FORMS). Разметку табло модель не
    #: генерирует: она выбирает вид, а рисует его панель.
    form = Column(String(16), nullable=False, default="number")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BoardShare(Base):
    """Поимённый доступ к доске: просмотр или редактирование."""
    __tablename__ = "board_shares"
    __table_args__ = (UniqueConstraint("board_id", "user_id", name="uq_board_share"),)

    id = Column(Integer, primary_key=True)
    board_id = Column(Integer, ForeignKey("boards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    right = Column(String(8), nullable=False, default=RIGHT_VIEW)


class Reminder(Base):
    """Разовое напоминание — отдельная способность вне знаний (спека #19).

    Живёт только с валидным абсолютным временем: без времени напоминание не
    создаётся, ассистент переспрашивает. Сработавшее остаётся помеченным
    (`reminded_at`) и убирается ретеншеном — руками его не закрывают.
    """
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    text = Column(Text, nullable=False)
    remind_at = Column(DateTime, nullable=False, index=True)
    reminded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class LegacyNote(Base):
    """Плоские заметки до знаний — переехали на доски миграцией 0008 (тикет #33).

    Таблица переименована, а не удалена: перенос разбирает свободный текст
    («в пятницу утром»), и человеку должно остаться куда посмотреть, если
    что-то переехало не туда. Кода, который её пишет или читает, больше нет —
    модель стоит здесь ради одной схемы у `create_all()` и у миграций.

    Виды заметок были: pref — предпочтение, health — здоровье, fact —
    наблюдение, task — напоминание.
    """
    __tablename__ = "notes_legacy"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    text = Column(Text, nullable=False)
    kind = Column(String(16), nullable=False, default="fact")
    source = Column(String(64), nullable=False, default="из разговора")
    pinned = Column(Boolean, nullable=False, default=False)

    #: Свободная формулировка («в пятницу утром») плюс точное время, если его удалось понять.
    when_text = Column(String(128), nullable=True)
    remind_at = Column(DateTime, nullable=True, index=True)
    reminded_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
