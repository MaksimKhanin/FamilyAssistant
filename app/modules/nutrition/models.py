"""Nutrition tables — strictly personal data, always scoped by user_id.

Calorie figures here are estimates by construction (a photo of a plate cannot give
a medical calculation), which is why every meal carries an explicit `status` and
`confidence`: the interface has to be able to say «≈ оценка» rather than presenting
a number as fact.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.core.db import Base

# --- meal status ---
STATUS_DRAFT = "draft"          # авто-оценка, ждёт подтверждения
STATUS_CONFIRMED = "confirmed"  # человек согласился с оценкой
STATUS_CORRECTED = "corrected"  # человек поправил цифры

STATUS_LABELS = {
    STATUS_DRAFT: "≈ оценка",
    STATUS_CONFIRMED: "подтверждено",
    STATUS_CORRECTED: "скорректировано вручную",
}

SOURCE_PHOTO = "photo"
SOURCE_TEXT = "text"

# --- goals ---
GOAL_LOSS = "loss"
GOAL_KEEP = "keep"
GOAL_GAIN = "gain"

GOAL_LABELS = {GOAL_LOSS: "снижение веса", GOAL_KEEP: "поддержание", GOAL_GAIN: "набор веса"}

#: kcal per unit, matching the live recalculation on the «Активность» screen.
ACTIVITY_KCAL = {"steps": 0.04, "walk": 4.0, "workout": 8.0, "bike": 7.0}
ACTIVITY_LABELS = {"steps": "Шаги", "walk": "Прогулка", "workout": "Тренировка", "bike": "Велосипед"}
ACTIVITY_UNITS = {"steps": "шагов", "walk": "мин", "workout": "мин", "bike": "мин"}

#: Потолок за одну запись — щедрый, но не бесконечный: без него опечатка вроде
#: «99999 шагов» тихо проходит и уводит баланс дня в область, которую никто не
#: ждал увидеть (см. UX-находку про абсурдные числа активности). Сутки — не
#: единица измерения дня, но 600 минут (10 часов) непрерывной активности и
#: 100000 шагов уже за пределами того, что вообще стоит записывать одной строкой.
ACTIVITY_CEILING = {"steps": 100000, "walk": 600, "workout": 600, "bike": 600}

#: Приёмы пищи, к которым предлагается блюдо. Свободную строку от модели
#: приводим к этому словарю, чтобы «Завтрак.» и «на завтрак» не расходились.
SLOTS = {"завтрак": "Завтрак", "обед": "Обед", "ужин": "Ужин", "перекус": "Перекус"}


class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    eaten_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    source = Column(String(8), nullable=False, default=SOURCE_TEXT)   # photo|text
    status = Column(String(16), nullable=False, default=STATUS_DRAFT)

    title = Column(String(128), nullable=False, default="Приём пищи")
    kcal = Column(Integer, nullable=False, default=0)
    protein = Column(Integer, nullable=False, default=0)
    fat = Column(Integer, nullable=False, default=0)
    carbs = Column(Integer, nullable=False, default=0)

    portion = Column(String(128), nullable=True)
    confidence = Column(String(8), nullable=True)      # low|medium|high — насколько уверена оценка
    raw_input = Column(Text, nullable=True)            # что человек написал
    image_path = Column(String(512), nullable=True)
    note = Column(String(255), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_estimate(self) -> bool:
        return self.status == STATUS_DRAFT


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    happened_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    kind = Column(String(16), nullable=False, default="steps")
    value = Column(Float, nullable=False, default=0)     # шаги или минуты
    kcal = Column(Integer, nullable=False, default=0)    # оценка потраченного
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def label(self) -> str:
        return ACTIVITY_LABELS.get(self.kind, self.kind)

    @property
    def unit(self) -> str:
        return ACTIVITY_UNITS.get(self.kind, "")


class NutritionProfile(Base):
    __tablename__ = "nutrition_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)

    daily_kcal = Column(Integer, nullable=False, default=2100)
    goal = Column(String(8), nullable=False, default=GOAL_KEEP)
    height_cm = Column(Integer, nullable=True)
    weight_kg = Column(Float, nullable=True)
    #: Пожелания к рациону, написанные человеком на экране «План питания»: что он
    #: ест, чего не ест, что любит. Не то же, что памятка про еду: памятка — про
    #: здоровье и цели и действует везде, а это — про вкусы и продукты, и нужно
    #: там, где блюда подбирают. В промпт они едут вместе (ADR-0010).
    preferences = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def goal_label(self) -> str:
        return GOAL_LABELS.get(self.goal, self.goal)


class MealIdea(Base):
    """Предложенное блюдо: то, что ассистент придумал, а человек, если понравилось, отметил.

    Едой это не становится: `Meal` — про съеденное, здесь же только идея. Поэтому
    в баланс дня и в статистику ничего отсюда не попадает, пока человек не запишет
    блюдо обычным `log_meal`.

    Одна таблица на две вещи, потому что вещь одна и та же — блюдо. Предложенное
    к какому-то дню рациона несёт `day_title`; отмеченное человеком («в закреп»)
    несёт `saved` и переживает следующий подбор. Блюдо из разговора приходит сюда
    сразу отмеченным и без дня.
    """
    __tablename__ = "meal_ideas"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    title = Column(String(128), nullable=False)
    slot = Column(String(16), nullable=True)          # завтрак | обед | ужин | перекус
    kcal = Column(Integer, nullable=False, default=0)
    #: К какому дню подбора относится блюдо («Завтра»). Пусто — блюдо живёт само
    #: по себе: отмеченное в разговоре или оставшееся от прошлого подбора.
    day_title = Column(String(32), nullable=True)
    position = Column(Integer, nullable=False, default=0)
    saved = Column(Boolean, nullable=False, default=False)
    #: Рецепт — по просьбе человека и не раньше: расписывать его каждому блюду
    #: подбора значит платить моделью за то, чего никто не открыл.
    recipe = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    @property
    def slot_label(self) -> str:
        return SLOTS.get((self.slot or "").lower(), self.slot or "")
