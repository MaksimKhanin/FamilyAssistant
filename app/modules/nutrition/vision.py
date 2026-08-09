"""Turning a photo or a sentence into a rough KБЖУ estimate.

This is the «Vision Pipeline / VLM» box of the architecture: it lives beside the
agent, not inside it. The agent decides *that* a meal should be estimated; this
module decides *what* the estimate is, and is the only place that knows how to
talk to a vision model.

Every result is explicitly an estimate — `confidence` is carried all the way to
the interface so a low-confidence guess can be shown as one.
"""
from dataclasses import dataclass
from typing import Optional

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
    return MealEstimate(
        title=title[:128],
        kcal=number("kcal"),
        protein=number("protein"),
        fat=number("fat"),
        carbs=number("carbs"),
        portion=(str(raw["portion"])[:128] if raw.get("portion") else None),
        confidence=confidence,
        note=(str(raw["note"])[:255] if raw.get("note") else None),
    )


def estimate_from_image(image_bytes: bytes, hint: str = None,
                        llm: LLMClient = None) -> MealEstimate:
    """Estimate a meal from a photo of the plate."""
    llm = llm or default_client
    prompt = "Что на фото и сколько это примерно?"
    if hint:
        prompt += f" Подсказка от человека: {hint}"

    raw = llm.json_completion(
        MEAL_VISION_SYSTEM,
        [text_part(prompt), image_part(image_bytes)],
        model=settings.llm.vision_model,
    )
    return _coerce(raw, fallback_title="Блюдо с фото")


def estimate_from_text(text: str, llm: LLMClient = None) -> MealEstimate:
    """Estimate a meal from a free-form description («кофе с молоком и бутерброд»)."""
    llm = llm or default_client
    raw = llm.json_completion(MEAL_TEXT_SYSTEM, text.strip())
    return _coerce(raw, fallback_title=text.strip()[:60] or "Приём пищи")


def safe_estimate_from_text(text: str, llm: LLMClient = None) -> MealEstimate:
    """Same as `estimate_from_text`, but never raises.

    When the model is unreachable the meal is still recorded — as a zero-calorie
    draft the person can fill in by hand. Losing the record entirely because a
    cloud endpoint blinked would be worse than an incomplete one.
    """
    try:
        return estimate_from_text(text, llm=llm)
    except LLMUnavailable:
        logger.warning("Модель недоступна — записываю приём пищи без оценки")
        return MealEstimate(title=text.strip()[:60] or "Приём пищи", kcal=0, protein=0, fat=0, carbs=0,
                            confidence="low", note="Оценку не удалось получить, поправьте цифры вручную")
