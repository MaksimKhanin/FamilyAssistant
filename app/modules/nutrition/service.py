"""Nutrition domain logic: meals, activity, daily balance, statistics."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.core import media
from app.core.clock import day_bounds_utc, local_date, local_today, utc_now
from app.core.templating import counted
from app.modules.nutrition.models import (
    ACTIVITY_KCAL, GOAL_KEEP, MEAL_FIELD_CEILING, NEAR_CEILING_RATIO, SLOTS, SOURCE_PHOTO,
    SOURCE_TEXT, STATUS_CONFIRMED, STATUS_CORRECTED, STATUS_DRAFT, ActivityLog, Meal, MealIdea,
    NutritionProfile,
)
from app.modules.nutrition.vision import MealEstimate

PERIODS = {"day": 1, "week": 7, "month": 30}
PERIOD_LABELS = {"day": "День", "week": "Неделя", "month": "Месяц"}

#: Тот же период словами — для фразы «убрал ...». Окно скользящее и всегда
#: включает сегодняшний день, поэтому «неделя» здесь честнее звучит как «последние
#: семь дней»: человек должен понимать, что именно исчезнет, до того как нажмёт.
PERIOD_WINDOWS = {"day": "за сегодня", "week": "за последние семь дней",
                  "month": "за последние 30 дней"}


# --- profile --------------------------------------------------------------

def get_profile(db: Session, user_id: int) -> NutritionProfile:
    profile = db.query(NutritionProfile).filter(NutritionProfile.user_id == user_id).one_or_none()
    if profile is None:
        profile = NutritionProfile(user_id=user_id, daily_kcal=2100, goal=GOAL_KEEP)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


#: Сколько человек может написать о своём рационе. Как и у памятки, ограничение
#: не про безопасность, а про контекст: этот текст уезжает в каждый подбор блюда.
PREFERENCES_LIMIT = 1200


def set_preferences(db: Session, user_id: int, text: str) -> NutritionProfile:
    """Пожелания к рациону: что человек ест, чего не ест, что любит.

    Пустой текст стирает написанное, а не хранит пустую строку, — иначе в промпт
    поехала бы строка «пожелания: », которая значит меньше, чем её отсутствие.
    """
    profile = get_profile(db, user_id)
    profile.preferences = (text or "").strip()[:PREFERENCES_LIMIT] or None
    db.commit()
    db.refresh(profile)
    return profile


def update_profile(db: Session, user_id: int, daily_kcal: int = None, goal: str = None,
                   height_cm: int = None, weight_kg: float = None) -> NutritionProfile:
    profile = get_profile(db, user_id)
    if daily_kcal is not None:
        profile.daily_kcal = max(1200, min(3400, int(daily_kcal)))
    if goal is not None:
        profile.goal = goal
    if height_cm is not None:
        profile.height_cm = height_cm
    if weight_kg is not None:
        profile.weight_kg = weight_kg
    db.commit()
    db.refresh(profile)
    return profile


# --- meals ----------------------------------------------------------------

def save_image(image_bytes: bytes, user_id: int) -> str:
    """Store a meal photo under MEDIA_ROOT/meals/<user>/<date>/ and return its path."""
    return media.store_bytes(image_bytes, "meals", str(user_id),
                             datetime.utcnow().strftime("%Y-%m-%d"))


def create_draft(db: Session, user_id: int, estimate: MealEstimate, source: str = SOURCE_TEXT,
                 raw_input: str = None, image_path: str = None, eaten_at: datetime = None) -> Meal:
    """Черновик из оценки модели или офлайн-разбора.

    Потолок зажимается уже здесь, а не только в `confirm_meal` при явной правке:
    иначе абсурдная цифра из модели («под миллиард ккал») попадает в таблицу
    нетронутой, и человек, подтвердивший черновик без единой цифры («да, всё
    верно»), молча уносит её в дневной и недельный баланс (см. `MEAL_FIELD_CEILING`).
    """
    meal = Meal(
        user_id=user_id,
        eaten_at=eaten_at or datetime.utcnow(),
        source=source if source in (SOURCE_PHOTO, SOURCE_TEXT) else SOURCE_TEXT,
        status=STATUS_DRAFT,
        title=estimate.title,
        kcal=max(0, min(MEAL_FIELD_CEILING["kcal"], estimate.kcal)),
        protein=max(0, min(MEAL_FIELD_CEILING["protein"], estimate.protein)),
        fat=max(0, min(MEAL_FIELD_CEILING["fat"], estimate.fat)),
        carbs=max(0, min(MEAL_FIELD_CEILING["carbs"], estimate.carbs)),
        portion=estimate.portion,
        confidence=estimate.confidence,
        raw_input=raw_input,
        image_path=image_path,
        note=estimate.note,
    )
    db.add(meal)
    db.commit()
    db.refresh(meal)
    return meal


def get_meal(db: Session, user_id: int, meal_id: int) -> Optional[Meal]:
    meal = db.get(Meal, meal_id)
    return meal if meal is not None and meal.user_id == user_id else None


def confirm_meal(db: Session, user_id: int, meal_id: int, corrections: dict = None) -> Optional[Meal]:
    """Finalise a draft. Any correction flips the status to «скорректировано вручную»."""
    meal = get_meal(db, user_id, meal_id)
    if meal is None:
        return None

    changed = False
    for field in ("kcal", "protein", "fat", "carbs"):
        value = (corrections or {}).get(field)
        if value is None:
            continue
        try:
            value = max(0, min(MEAL_FIELD_CEILING[field], int(round(float(value)))))
        except (TypeError, ValueError):
            continue
        if value != getattr(meal, field):
            setattr(meal, field, value)
            changed = True

    title = (corrections or {}).get("title")
    if title and title.strip() and title.strip() != meal.title:
        meal.title = title.strip()[:128]
        changed = True

    meal.status = STATUS_CORRECTED if changed else STATUS_CONFIRMED
    db.commit()
    db.refresh(meal)
    return meal


def delete_meal(db: Session, user_id: int, meal_id: int) -> bool:
    meal = get_meal(db, user_id, meal_id)
    if meal is None:
        return False
    # Снимок тарелки живёт ровно столько, сколько запись о ней: без строки в базе
    # его всё равно никто не откроет, а место он занимает.
    media.discard(meal.image_path)
    db.delete(meal)
    db.commit()
    return True


def last_meal(db: Session, user_id: int) -> Optional[Meal]:
    """Самая свежая запись о еде — то, что человек имеет в виду под «удали последнее»."""
    return (
        db.query(Meal)
        .filter(Meal.user_id == user_id)
        .order_by(Meal.eaten_at.desc(), Meal.id.desc())
        .first()
    )


def meals_for_day(db: Session, user_id: int, day: date = None) -> List[Meal]:
    start, end = day_bounds_utc(day or local_today())
    return (
        db.query(Meal)
        .filter(Meal.user_id == user_id, Meal.eaten_at >= start, Meal.eaten_at < end)
        .order_by(Meal.eaten_at)
        .all()
    )


# --- activity -------------------------------------------------------------

def estimate_activity_kcal(kind: str, value: float) -> int:
    return int(round(ACTIVITY_KCAL.get(kind, 0.0) * float(value or 0)))


def log_activity(db: Session, user_id: int, kind: str, value: float,
                 happened_at: datetime = None) -> ActivityLog:
    entry = ActivityLog(
        user_id=user_id,
        kind=kind,
        value=float(value or 0),
        kcal=estimate_activity_kcal(kind, value),
        happened_at=happened_at or datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_activity(db: Session, user_id: int, activity_id: int) -> Optional[ActivityLog]:
    entry = db.get(ActivityLog, activity_id)
    return entry if entry is not None and entry.user_id == user_id else None


def last_activity(db: Session, user_id: int) -> Optional[ActivityLog]:
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id)
        .order_by(ActivityLog.happened_at.desc(), ActivityLog.id.desc())
        .first()
    )


def delete_activity(db: Session, user_id: int, activity_id: int) -> bool:
    entry = get_activity(db, user_id, activity_id)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    return True


def activity_for_day(db: Session, user_id: int, day: date = None) -> List[ActivityLog]:
    start, end = day_bounds_utc(day or local_today())
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == user_id,
                ActivityLog.happened_at >= start,
                ActivityLog.happened_at < end)
        .order_by(ActivityLog.happened_at)
        .all()
    )


# --- statistics -----------------------------------------------------------

@dataclass
class DayTotals:
    day: date
    consumed: int = 0
    burned: int = 0
    protein: int = 0
    fat: int = 0
    carbs: int = 0

    @property
    def balance(self) -> int:
        return self.consumed - self.burned


@dataclass
class PeriodStats:
    period: str
    days: List[DayTotals]
    norm: int
    #: Хотя бы одна запись в периоде уже на краю MEAL_FIELD_CEILING — потолок не
    #: даёт цифре стать бесконечной, но «на пределе того, что вообще стоит
    #: записывать» и «немного больше нормы» звучать одинаково нейтрально не должны.
    near_ceiling: bool = False

    @property
    def consumed(self) -> int:
        return sum(d.consumed for d in self.days)

    @property
    def burned(self) -> int:
        return sum(d.burned for d in self.days)

    @property
    def balance(self) -> int:
        return self.consumed - self.burned

    @property
    def avg_consumed(self) -> int:
        return round(self.consumed / len(self.days)) if self.days else 0

    @property
    def macros(self) -> Dict[str, int]:
        return {
            "protein": sum(d.protein for d in self.days),
            "fat": sum(d.fat for d in self.days),
            "carbs": sum(d.carbs for d in self.days),
        }

    @property
    def today(self) -> DayTotals:
        return self.days[-1] if self.days else DayTotals(day=local_today())


def period_stats(db: Session, user_id: int, period: str = "day", today: date = None) -> PeriodStats:
    """Per-day consumed/burned totals for the requested window, oldest day first."""
    span = PERIODS.get(period, 1)
    today = today or local_today()
    first_day = today - timedelta(days=span - 1)
    start, _ = day_bounds_utc(first_day)

    buckets = {first_day + timedelta(days=i): DayTotals(day=first_day + timedelta(days=i)) for i in range(span)}

    near_ceiling = False
    for meal in db.query(Meal).filter(Meal.user_id == user_id, Meal.eaten_at >= start).all():
        bucket = buckets.get(local_date(meal.eaten_at))
        if bucket is None:
            continue
        bucket.consumed += meal.kcal
        bucket.protein += meal.protein
        bucket.fat += meal.fat
        bucket.carbs += meal.carbs
        if any(getattr(meal, field) >= NEAR_CEILING_RATIO * ceiling
               for field, ceiling in MEAL_FIELD_CEILING.items()):
            near_ceiling = True

    for entry in db.query(ActivityLog).filter(ActivityLog.user_id == user_id,
                                              ActivityLog.happened_at >= start).all():
        bucket = buckets.get(local_date(entry.happened_at))
        if bucket is not None:
            bucket.burned += entry.kcal

    profile = get_profile(db, user_id)
    return PeriodStats(period=period, days=[buckets[k] for k in sorted(buckets)], norm=profile.daily_kcal,
                       near_ceiling=near_ceiling)


# --- журнал: что именно записано за эти дни -------------------------------

@dataclass
class DayRecords:
    """Один календарный день семьи со всем, что в нём записано.

    Цифры дня складываются из строк, и человеку нужны обе стороны: увидеть, из
    чего вышел столбик на графике, и убрать оттуда лишнее. Поэтому день здесь —
    не итог (`DayTotals`), а сами записи.
    """
    day: date
    meals: List[Meal] = field(default_factory=list)
    activity: List[ActivityLog] = field(default_factory=list)

    @property
    def consumed(self) -> int:
        return sum(m.kcal for m in self.meals)

    @property
    def burned(self) -> int:
        return sum(a.kcal for a in self.activity)

    @property
    def count(self) -> int:
        return len(self.meals) + len(self.activity)


def records_for_period(db: Session, user_id: int, period: str = "week",
                       today: date = None) -> List[DayRecords]:
    """Записи периода, разложенные по дням; свежий день первым.

    Пустые дни выпадают: на графике нулевой столбик — это осмысленный ноль, а в
    списке пустой день — просто строка, которую пролистывают.
    """
    span = PERIODS.get(period, 1)
    today = today or local_today()
    start, _ = day_bounds_utc(today - timedelta(days=span - 1))
    _, end = day_bounds_utc(today)

    days: Dict[date, DayRecords] = {}

    def bucket(moment: datetime) -> DayRecords:
        day = local_date(moment)
        if day not in days:
            days[day] = DayRecords(day=day)
        return days[day]

    for meal in (db.query(Meal)
                 .filter(Meal.user_id == user_id, Meal.eaten_at >= start, Meal.eaten_at < end)
                 .order_by(Meal.eaten_at).all()):
        bucket(meal.eaten_at).meals.append(meal)

    for entry in (db.query(ActivityLog)
                  .filter(ActivityLog.user_id == user_id,
                          ActivityLog.happened_at >= start, ActivityLog.happened_at < end)
                  .order_by(ActivityLog.happened_at).all()):
        bucket(entry.happened_at).activity.append(entry)

    return [days[day] for day in sorted(days, reverse=True)]


# --- чистка: убрать записанное пачкой --------------------------------------

WHAT_MEALS = "meals"
WHAT_ACTIVITY = "activity"
WHAT_ALL = "all"

#: Что именно чистим. «Всё» — это и еда, и активность: человек, который просит
#: убрать статистику за неделю, имеет в виду обе половины баланса.
WHAT_LABELS = {
    WHAT_MEALS: "записи о еде",
    WHAT_ACTIVITY: "записи об активности",
    WHAT_ALL: "записи о еде и активности",
}


@dataclass
class Removed:
    """Сколько записей убрано — раздельно, чтобы сказать об этом человеку точно."""
    meals: int = 0
    activity: int = 0

    @property
    def total(self) -> int:
        return self.meals + self.activity

    def __bool__(self) -> bool:
        return self.total > 0

    @property
    def words(self) -> str:
        """«3 приёма пищи и 1 запись активности» — одинаково в панели и в чате.

        Пересказывать удалённое двумя разными фразами незачем: человек читает их
        в один день на одном экране, а числа тут — единственное, что успокаивает.
        """
        parts = []
        if self.meals:
            parts.append(counted(self.meals, "приём пищи", "приёма пищи", "приёмов пищи"))
        if self.activity:
            parts.append(counted(self.activity, "запись активности", "записи активности",
                                 "записей активности"))
        return " и ".join(parts) or "ничего"


def _clear_window(db: Session, user_id: int, start: datetime, end: datetime,
                  what: str = WHAT_ALL) -> Removed:
    """Убрать записи в окне [start, end) — общая работа для дня и для периода."""
    removed = Removed()

    if what in (WHAT_MEALS, WHAT_ALL):
        meals = (db.query(Meal)
                 .filter(Meal.user_id == user_id, Meal.eaten_at >= start, Meal.eaten_at < end)
                 .all())
        for meal in meals:
            media.discard(meal.image_path)
            db.delete(meal)
        removed.meals = len(meals)

    if what in (WHAT_ACTIVITY, WHAT_ALL):
        entries = (db.query(ActivityLog)
                   .filter(ActivityLog.user_id == user_id,
                           ActivityLog.happened_at >= start, ActivityLog.happened_at < end)
                   .all())
        for entry in entries:
            db.delete(entry)
        removed.activity = len(entries)

    db.commit()
    return removed


def clear_day(db: Session, user_id: int, day: date, what: str = WHAT_ALL) -> Removed:
    """Убрать всё записанное за один календарный день семьи."""
    start, end = day_bounds_utc(day)
    return _clear_window(db, user_id, start, end, what)


def clear_period(db: Session, user_id: int, period: str = "day", what: str = WHAT_ALL,
                 today: date = None) -> Removed:
    """Убрать записи за тот же период, каким считается статистика.

    Окно то же, что у `period_stats`: «день» — сегодняшний, «неделя» — последние
    семь суток вместе с сегодняшними. Иначе человек убирал бы не то, что видит.
    """
    span = PERIODS.get(period, 1)
    today = today or local_today()
    start, _ = day_bounds_utc(today - timedelta(days=span - 1))
    _, end = day_bounds_utc(today)
    return _clear_window(db, user_id, start, end, what)


# --- идеи блюд: подбор на экране и отмеченное человеком ---------------------

@dataclass
class PlanDay:
    """День подбора со своими блюдами — то, что рисует экран «План питания»."""
    title: str
    ideas: List[MealIdea] = field(default_factory=list)

    @property
    def kcal(self) -> int:
        return sum(idea.kcal for idea in self.ideas)


def _clean_slot(slot: str) -> Optional[str]:
    """Приём пищи словом из словаря — или ничего. Модель пишет по-разному."""
    lowered = (slot or "").strip().lower().strip(".")
    return next((key for key in SLOTS if key in lowered), None)


def add_idea(db: Session, user_id: int, title: str, slot: str = None, kcal: int = 0,
             day_title: str = None, position: int = 0, saved: bool = False) -> Optional[MealIdea]:
    title = (title or "").strip()[:128]
    if not title:
        return None
    idea = MealIdea(
        user_id=user_id,
        title=title,
        slot=_clean_slot(slot),
        kcal=max(0, int(kcal or 0)),
        day_title=(day_title or "").strip()[:32] or None,
        position=position,
        saved=saved,
    )
    db.add(idea)
    db.commit()
    db.refresh(idea)
    return idea


def keep_dish(db: Session, user_id: int, title: str, slot: str = None,
              kcal: int = 0) -> Optional[MealIdea]:
    """Отметить блюдо из разговора. Нажали дважды — блюдо всё равно одно.

    Кнопка живёт в ленте чата и остаётся там навсегда: человек пролистывает
    вчерашний разговор и нажимает ещё раз, а второе «то же самое» в закрепе — это
    не память ассистента, а его невнимательность.
    """
    title = (title or "").strip()
    if not title:
        return None
    existing = next((idea for idea in saved_ideas(db, user_id)
                     if idea.title.lower() == title.lower()[:128]), None)
    return existing or add_idea(db, user_id, title, slot=slot, kcal=kcal, saved=True)


def replace_plan(db: Session, user_id: int, days: List[dict]) -> List[PlanDay]:
    """Положить новый подбор на место прежнего, не тронув отмеченное.

    Неотмеченные блюда прошлого подбора исчезают: «предложить другое» — это
    просьба заменить, а не дописать. Отмеченные остаются жить, но теряют свой
    день: они больше не часть подбора, который человек видит, а его закреп.
    """
    stale = db.query(MealIdea).filter(MealIdea.user_id == user_id,
                                      MealIdea.day_title.isnot(None)).all()
    for idea in stale:
        if idea.saved:
            idea.day_title = None
        else:
            db.delete(idea)
    db.commit()

    for day_number, day in enumerate(days):
        for number, meal in enumerate(day.get("meals") or []):
            add_idea(db, user_id, meal.get("name"), slot=meal.get("slot"),
                     kcal=meal.get("kcal") or 0, day_title=day.get("title") or "День",
                     position=day_number * 100 + number)
    return plan_days(db, user_id)


def plan_days(db: Session, user_id: int) -> List[PlanDay]:
    """Последний подбор, разложенный по дням в том порядке, в каком его собрали."""
    rows = (
        db.query(MealIdea)
        .filter(MealIdea.user_id == user_id, MealIdea.day_title.isnot(None))
        .order_by(MealIdea.position, MealIdea.id)
        .all()
    )
    days: Dict[str, PlanDay] = {}
    for idea in rows:
        days.setdefault(idea.day_title, PlanDay(title=idea.day_title)).ideas.append(idea)
    return list(days.values())


def saved_ideas(db: Session, user_id: int, limit: int = 50) -> List[MealIdea]:
    """Закреп: блюда, которые человек отметил, — свежие первыми."""
    return (
        db.query(MealIdea)
        .filter(MealIdea.user_id == user_id, MealIdea.saved.is_(True))
        .order_by(MealIdea.created_at.desc(), MealIdea.id.desc())
        .limit(limit)
        .all()
    )


def saved_titles(db: Session, user_id: int, limit: int = 12) -> List[str]:
    """Отмеченное — короткими строками для промпта: это то, что человеку зашло."""
    return [f"{idea.title} (≈{idea.kcal} ккал)" if idea.kcal else idea.title
            for idea in saved_ideas(db, user_id, limit=limit)]


def get_idea(db: Session, user_id: int, idea_id: int) -> Optional[MealIdea]:
    idea = db.get(MealIdea, idea_id)
    return idea if idea is not None and idea.user_id == user_id else None


def toggle_saved(db: Session, user_id: int, idea_id: int) -> Optional[MealIdea]:
    """Отметить блюдо или снять отметку — одна и та же кнопка.

    Снятая отметка у блюда, которого нет в текущем подборе, — это «убрать из
    закрепа»: держать такую строку больше не за что, и она удаляется. У блюда из
    подбора отметка просто гаснет: сам подбор никуда не делся.

    Модель вправе повторить в новом подборе название, которое человек уже
    закрепил (см. `replace_plan`) — та строка из дня остаётся его копией, пока
    её не отметили. Отметить её — не завести второй закреп, а обнаружить, что
    он уже есть: как и `keep_dish`, ищем совпадение по названию среди уже
    закреплённого и переиспользуем его вместо дубля.
    """
    idea = get_idea(db, user_id, idea_id)
    if idea is None:
        return None
    if not idea.saved:
        existing = next((other for other in saved_ideas(db, user_id)
                         if other.id != idea.id and other.title.lower() == idea.title.lower()),
                        None)
        if existing is not None:
            db.delete(idea)
            db.commit()
            return existing
    idea.saved = not idea.saved
    if not idea.saved and idea.day_title is None:
        db.delete(idea)
        db.commit()
        return None
    db.commit()
    db.refresh(idea)
    return idea


def set_recipe(db: Session, user_id: int, idea_id: int, recipe: str) -> Optional[MealIdea]:
    idea = get_idea(db, user_id, idea_id)
    if idea is None:
        return None
    idea.recipe = (recipe or "").strip() or None
    db.commit()
    db.refresh(idea)
    return idea


def recent_meal_titles(db: Session, user_id: int, days: int = 14, limit: int = 25) -> List[str]:
    since = utc_now() - timedelta(days=days)
    rows = (
        db.query(Meal)
        .filter(Meal.user_id == user_id, Meal.eaten_at >= since)
        .order_by(Meal.eaten_at.desc())
        .limit(limit)
        .all()
    )
    return [f"{m.title} (≈{m.kcal} ккал)" for m in rows]
