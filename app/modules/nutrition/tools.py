"""Agent tools for the nutrition module.

`log_meal` never writes a final record: it produces a draft with an explicit
estimate, and `confirm_meal` is what turns it into a confirmed (or hand-corrected)
entry. That two-step shape is a product requirement — the number came from a model
looking at a plate, and the person must be able to fix it in one gesture.
"""
from app.agent.llm import LLMUnavailable, client as llm_client
from app.agent.prompts import MEAL_PLAN_SYSTEM
from app.agent.registry import ToolContext, ToolResult, tool
from app.core.events import ACTIVITY_LOGGED, MEAL_CONFIRMED, MEAL_LOGGED, bus
from app.core.logging import get_logger
from app.modules.memory import service as memory_service
from app.modules.nutrition import service
from app.modules.nutrition.models import (
    ACTIVITY_KCAL, ACTIVITY_LABELS, ACTIVITY_UNITS, SOURCE_PHOTO, SOURCE_TEXT,
)
from app.modules.nutrition.vision import estimate_from_image, safe_estimate_from_text

MODULE = "nutrition"
logger = get_logger("nutrition.tools")


def _meal_card(meal, subtitle: str = None) -> dict:
    return {
        "type": "meal",
        "meal_id": meal.id,
        "title": meal.title,
        "kcal": meal.kcal,
        "protein": meal.protein,
        "fat": meal.fat,
        "carbs": meal.carbs,
        "status": meal.status,
        "is_estimate": meal.is_estimate,
        "subtitle": subtitle or meal.portion or "",
    }


@tool(
    name="log_meal",
    module=MODULE,
    title="Записать приём пищи",
    description="""
    Оценить и записать съеденное. Если человек прислал фото — оценка идёт по фото,
    текст можно передать как уточнение. Если фото нет, передай в text описание
    своими словами («кофе с молоком и бутерброд»).
    Запись создаётся черновиком с пометкой «оценка»: цифры ещё не окончательные,
    их подтверждает человек. После вызова скажи, что получилось, и предложи поправить.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Что человек съел, его словами"},
        },
    },
    auto_from=2,
)
def log_meal(ctx: ToolContext, text: str = None) -> ToolResult:
    image = ctx.attachments.get("image")

    if image:
        try:
            estimate = estimate_from_image(image, hint=text)
        except LLMUnavailable:
            return ToolResult(
                summary="Не смог разглядеть блюдо — модель не отвечает. Опишите словами, я запишу.",
                ok=False,
            )
        image_path = service.save_image(image, ctx.subject.id)
        meal = service.create_draft(ctx.db, ctx.subject.id, estimate, source=SOURCE_PHOTO,
                                    raw_input=text, image_path=image_path)
    else:
        if not (text or "").strip():
            return ToolResult(summary="Нечего записывать: нет ни фото, ни описания.", ok=False)
        estimate = safe_estimate_from_text(text)
        meal = service.create_draft(ctx.db, ctx.subject.id, estimate, source=SOURCE_TEXT, raw_input=text)

    bus.publish(MEAL_LOGGED, {"meal_id": meal.id, "user_id": ctx.subject.id})

    hedge = "" if estimate.confidence != "low" else " Уверенности мало, посмотрите цифры."
    return ToolResult(
        summary=(f"Записал черновиком: {meal.title} — примерно {meal.kcal} ккал "
                 f"(Б {meal.protein} / Ж {meal.fat} / У {meal.carbs}). "
                 f"Это оценка, ждёт подтверждения (meal_id={meal.id}).{hedge}"),
        data={"meal_id": meal.id, "kcal": meal.kcal, "confidence": estimate.confidence},
        card=_meal_card(meal, subtitle=estimate.portion),
    )


@tool(
    name="confirm_meal",
    module=MODULE,
    title="Подтвердить запись",
    description="""
    Подтвердить черновик приёма пищи или поправить его цифры. Передавай только те
    поля, которые человек назвал; остальные останутся как есть. Если цифры изменились,
    запись помечается как скорректированная вручную.
    """,
    parameters={
        "type": "object",
        "properties": {
            "meal_id": {"type": "integer"},
            "kcal": {"type": "integer"},
            "protein": {"type": "integer"},
            "fat": {"type": "integer"},
            "carbs": {"type": "integer"},
            "title": {"type": "string", "description": "Уточнённое название блюда"},
        },
        "required": ["meal_id"],
    },
    auto_from=2,
)
def confirm_meal(ctx: ToolContext, meal_id: int, kcal: int = None, protein: int = None,
                 fat: int = None, carbs: int = None, title: str = None) -> ToolResult:
    corrections = {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs, "title": title}
    meal = service.confirm_meal(ctx.db, ctx.subject.id, meal_id,
                                {k: v for k, v in corrections.items() if v is not None})
    if meal is None:
        return ToolResult(summary="Такой записи нет.", ok=False)

    bus.publish(MEAL_CONFIRMED, {"meal_id": meal.id, "user_id": ctx.subject.id})
    return ToolResult(
        summary=f"Записал: {meal.title} — {meal.kcal} ккал ({meal.status_label}).",
        data={"meal_id": meal.id},
        card=_meal_card(meal),
    )


@tool(
    name="log_activity",
    module=MODULE,
    title="Записать активность",
    description="""
    Записать активность и оценить потраченные калории.
    kind: steps — шаги (value в шагах), walk — прогулка, workout — тренировка,
    bike — велосипед (value в минутах).
    """,
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": list(ACTIVITY_KCAL)},
            "value": {"type": "number", "description": "Количество шагов или минут"},
        },
        "required": ["kind", "value"],
    },
    auto_from=2,
)
def log_activity(ctx: ToolContext, kind: str, value: float) -> ToolResult:
    if kind not in ACTIVITY_KCAL:
        return ToolResult(summary=f"Не знаю такой вид активности: {kind}", ok=False)

    entry = service.log_activity(ctx.db, ctx.subject.id, kind, value)
    bus.publish(ACTIVITY_LOGGED, {"activity_id": entry.id, "user_id": ctx.subject.id})

    unit = ACTIVITY_UNITS.get(kind, "")
    return ToolResult(
        summary=(f"Записал: {ACTIVITY_LABELS[kind].lower()} — {int(entry.value)} {unit}, "
                 f"примерно {entry.kcal} ккал."),
        data={"activity_id": entry.id, "kcal": entry.kcal},
    )


@tool(
    name="get_nutrition_stats",
    module=MODULE,
    title="Показать статистику",
    description="""
    Сводка по питанию за период: получено, потрачено, баланс и суточная норма.
    period: day | week | month.
    """,
    parameters={
        "type": "object",
        "properties": {"period": {"type": "string", "enum": ["day", "week", "month"]}},
    },
    read_only=True,
)
def get_nutrition_stats(ctx: ToolContext, period: str = "day") -> ToolResult:
    stats = service.period_stats(ctx.db, ctx.subject.id, period)
    macros = stats.macros

    return ToolResult(
        summary=(f"За период «{period}»: получено {stats.consumed} ккал, потрачено {stats.burned}, "
                 f"баланс {stats.balance:+d}. Норма — {stats.norm} ккал в день, "
                 f"в среднем выходит {stats.avg_consumed}. "
                 f"Б {macros['protein']} / Ж {macros['fat']} / У {macros['carbs']}."),
        data={"consumed": stats.consumed, "burned": stats.burned, "balance": stats.balance},
        card={
            "type": "stats",
            "period": period,
            "consumed": stats.consumed,
            "burned": stats.burned,
            "norm": stats.norm,
        },
    )


@tool(
    name="suggest_meal_plan",
    module=MODULE,
    title="Предложить идеи питания",
    description="""
    Предложить идеи питания на ближайшие дни с опорой на историю, цель и то, что
    записано в памяти (предпочтения, аллергии). Это идеи, а не предписание — так и подавай.
    """,
    parameters={"type": "object", "properties": {}},
    auto_from=1,
)
def suggest_meal_plan(ctx: ToolContext) -> ToolResult:
    profile = service.get_profile(ctx.db, ctx.subject.id)
    history = service.recent_meal_titles(ctx.db, ctx.subject.id)
    notes = memory_service.search_notes(ctx.db, ctx.subject.id, limit=10)

    prompt = (
        f"Человек: {ctx.subject.display_name}. Цель: {profile.goal_label}. "
        f"Суточная норма: {profile.daily_kcal} ккал.\n"
        f"Что ел за последние дни: {', '.join(history) if history else 'записей пока нет'}.\n"
        f"Что известно из памяти: "
        f"{'; '.join(n.text for n in notes) if notes else 'ничего особенного'}."
    )

    try:
        raw = llm_client.json_completion(MEAL_PLAN_SYSTEM, prompt, max_tokens=900)
    except LLMUnavailable:
        return ToolResult(summary="Сейчас не могу собрать идеи — модель не отвечает.", ok=False)

    days = []
    for day in (raw.get("days") or [])[:3]:
        meals = [
            {"name": str(m.get("name", ""))[:80],
             "slot": str(m.get("slot", ""))[:16],
             "kcal": int(m.get("kcal") or 0)}
            for m in (day.get("meals") or [])[:4]
            if m.get("name")
        ]
        if meals:
            days.append({"title": str(day.get("title", ""))[:32] or "День",
                         "kcal": int(day.get("kcal") or sum(m["kcal"] for m in meals)),
                         "meals": meals})

    if not days:
        return ToolResult(summary="Не получилось собрать идеи — попробуем ещё раз?", ok=False)

    comment = str(raw.get("comment") or "").strip()
    lines = [f"{d['title']}: " + ", ".join(m["name"] for m in d["meals"]) + f" (≈{d['kcal']} ккал)" for d in days]
    return ToolResult(
        summary=("Идеи на ближайшие дни:\n" + "\n".join(lines) + (f"\n{comment}" if comment else "")),
        data={"days": days},
        card={"type": "plan", "days": days, "comment": comment},
    )
