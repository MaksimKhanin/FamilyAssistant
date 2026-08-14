"""Turning a photo or a sentence into a rough KБЖУ estimate.

This is the «Vision Pipeline / VLM» box of the architecture: it lives beside the
agent, not inside it. The agent decides *that* a meal should be estimated; this
module decides *what* the estimate is, and is the only place that knows how to
talk to a vision model.

Every result is explicitly an estimate — `confidence` is carried all the way to
the interface so a low-confidence guess can be shown as one.
"""
from dataclasses import dataclass, field
from typing import List, Optional

from app.agent.llm import LLMClient, LLMUnavailable, client as default_client, image_part, text_part
from app.agent.prompts import MEAL_TEXT_SYSTEM, MEAL_VISION_SYSTEM
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("nutrition.vision")

CONFIDENCE_LABELS = {"low": "оценка приблизительная", "medium": "оценка", "high": "оценка уверенная"}


@dataclass
class MealEstimate:
    title: str
    kcal: int
    protein: int
    fat: int
    carbs: int
    portion: Optional[str] = None
    confidence: str = "medium"
    note: Optional[str] = None
    #: Из чего сложилась цифра: «борщ ~300 г», «сметана ~20 г». Человеку это
    #: объясняет оценку лучше любого «уверенность средняя».
    components: List[str] = field(default_factory=list)
    #: Один вопрос, ответ на который заметно сдвинет цифры. Пусто — спрашивать нечего.
    question: Optional[str] = None
    #: Фабричный товар, узнанный в описании или на фото: «батончик Mars 51 г».
    #: Повод сходить за составом в интернет вместо счёта на глаз (`lookup.py`).
    brand: Optional[str] = None
    #: Адреса, откуда взяты цифры, если считали по справке из интернета.
    sources: List[str] = field(default_factory=list)

    @property
    def confidence_label(self) -> str:
        return CONFIDENCE_LABELS.get(self.confidence, "оценка")


def _coerce(raw: dict, fallback_title: str) -> MealEstimate:
    def number(key: str) -> int:
        value = raw.get(key, 0)
        try:
            return max(0, int(round(float(value))))
        except (TypeError, ValueError):
            return 0

    confidence = str(raw.get("confidence", "medium")).lower()
    if confidence not in CONFIDENCE_LABELS:
        confidence = "medium"

    title = str(raw.get("title") or "").strip() or fallback_title
    components = [str(item)[:64] for item in (raw.get("components") or [])[:8] if str(item).strip()]
    question = str(raw.get("question") or "").strip()
    brand = str(raw.get("brand") or "").strip()

    return MealEstimate(
        title=title[:128],
        kcal=number("kcal"),
        protein=number("protein"),
        fat=number("fat"),
        carbs=number("carbs"),
        portion=(str(raw["portion"])[:128] if raw.get("portion") else None),
        confidence=confidence,
        note=(str(raw["note"])[:255] if raw.get("note") else None),
        components=components,
        question=question[:160] or None,
        brand=brand[:96] or None,
    )


def estimate_from_image(image_bytes: bytes, hint: str = None, context: str = None,
                        facts: str = None, llm: LLMClient = None) -> MealEstimate:
    """Estimate a meal from a photo of the plate.

    `facts` — справка о фабричном товаре, найденная в интернете (`lookup.py`). Если
    она есть, промпт велит считать по ней: у товара с этикеткой состав известен, и
    гадать по внешнему виду упаковки незачем.
    """
    llm = llm or default_client
    prompt = "Что на фото и сколько это примерно?"
    if hint:
        prompt += f" Подсказка от человека: {hint}"
    if context:
        prompt += f"\n\n{context}"
    if facts:
        prompt += f"\n\n{facts}"

    raw = llm.json_completion(
        MEAL_VISION_SYSTEM,
        [text_part(prompt), image_part(image_bytes)],
        model=settings.llm.vision_model,
    )
    return _coerce(raw, fallback_title="Блюдо с фото")


def estimate_from_text(text: str, context: str = None, facts: str = None,
                       llm: LLMClient = None) -> MealEstimate:
    """Estimate a meal from a free-form description («кофе с молоком и бутерброд»).

    `context` — что известно об этом человеке: цель, норма, записи с досок знаний.
    Аллергия или «ест без сахара» меняют оценку сильнее, чем кажется, а модель
    сама об этом не спросит.

    `facts` — справка о фабричном товаре из интернета, см. `estimate_from_image`.
    """
    llm = llm or default_client
    prompt = text.strip()
    if context:
        prompt += f"\n\n{context}"
    if facts:
        prompt += f"\n\n{facts}"
    raw = llm.json_completion(MEAL_TEXT_SYSTEM, prompt)
    return _coerce(raw, fallback_title=text.strip()[:60] or "Приём пищи")


def safe_estimate_from_text(text: str, context: str = None, facts: str = None,
                            llm: LLMClient = None) -> MealEstimate:
    """Same as `estimate_from_text`, but never raises.

    When the model is unreachable the meal is still recorded — as a zero-calorie
    draft the person can fill in by hand. Losing the record entirely because a
    cloud endpoint blinked would be worse than an incomplete one.
    """
    try:
        return estimate_from_text(text, context=context, facts=facts, llm=llm)
    except LLMUnavailable:
        logger.warning("Модель недоступна — записываю приём пищи без оценки")
        return MealEstimate(title=text.strip()[:60] or "Приём пищи", kcal=0, protein=0, fat=0, carbs=0,
                            confidence="low", note="Оценку не удалось получить, поправьте цифры вручную")
