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
from app.modules.memory import knowledge
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


def _person_context(ctx: ToolContext) -> str:
    """Что оценщику полезно знать про этого человека.

    Без этого блока модель считает еду «в вакууме»: не знает ни цели, ни того, что
    человек не ест сахар. Записи с досок влияют на оценку сильнее, чем кажется.

    Профиль берётся у того, кому считают еду (`subject`), а знания — у того, кто
    разговаривает (`actor`): доски из режима «от лица» исключены, и ассистент
    видит ровно те, что видит его собеседник (ADR-0005).
    """
    profile = service.get_profile(ctx.db, ctx.subject.id)
    notes = _known(ctx)
    lines = [
        "Что известно об этом человеке (учитывай, но не упоминай в ответе):",
        f"- цель: {profile.goal_label}, суточная норма {profile.daily_kcal} ккал",
    ]
    if profile.weight_kg:
        lines.append(f"- вес: {profile.weight_kg:g} кг")
    if notes:
        lines.append(f"- с досок {ctx.actor.display_name}: " + "; ".join(notes))
    return "\n".join(lines)


def _known(ctx: ToolContext, limit: int = 8) -> list:
    """Факты с досок собеседника — короткими строками для промпта.

    Чьи это доски, в промпте сказано прямо: профиль тут одного человека, а
    знания другого, когда глава семьи считает еду за ребёнка (ADR-0005).
    """
    return knowledge.person_facts(ctx.db, ctx.actor.id, limit=limit)


def _describe(text: str, weight_g: float = None, cooking: str = None) -> str:
    """Слова человека плюс то, что он уточнил отдельными полями."""
    parts = [(text or "").strip()]
    if weight_g:
        parts.append(f"вес порции примерно {int(weight_g)} г")
    if cooking:
        parts.append(f"приготовлено: {cooking.strip()}")
    return ", ".join(part for part in parts if part)


@tool(
    name="log_meal",
    module=MODULE,
    title="Записать приём пищи",
    description="""
    Оценить и записать съеденное. Если человек прислал фото — оценка идёт по фото,
    текст можно передать как уточнение. Если фото нет, передай в text фразу человека
    целиком («съел тарелку борща со сметаной»), а не одно слово из неё.
    weight_g и cooking заполняй, только если человек их назвал, — они заметно
    уточняют оценку.
    Запись создаётся черновиком с пометкой «оценка»: цифры ещё не окончательные,
    их подтверждает человек. После вызова назови цифры и, если в ответе есть вопрос,
    задай его одной фразой.
    """,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Что человек съел, его словами, фразой целиком"},
            "weight_g": {"type": "number",
                         "description": "Вес или объём порции в граммах, если человек его назвал"},
            "cooking": {"type": "string",
                        "description": "Способ приготовления, если назван: жареное, варёное, на пару, "
                                       "с маслом, без сахара"},
        },
    },
    # Ноль, а не два: инструмент и так пишет только черновик, который человек
    # подтверждает карточкой (confirm_meal). Ещё одно подтверждение поверх этого
    # означало бы, что оценка не считается вовсе, — и человеку нечего подтверждать.
    auto_from=0,
)
def log_meal(ctx: ToolContext, text: str = None, weight_g: float = None,
             cooking: str = None) -> ToolResult:
    image = ctx.attachments.get("image")
    context = _person_context(ctx)
    described = _describe(text, weight_g, cooking)

    if image:
        try:
            estimate = estimate_from_image(image, hint=described or None, context=context)
        except LLMUnavailable:
            return ToolResult(
                summary="Не смог разглядеть блюдо — модель не отвечает. Опишите словами, я запишу.",
                ok=False,
            )
        image_path = service.save_image(image, ctx.subject.id)
        meal = service.create_draft(ctx.db, ctx.subject.id, estimate, source=SOURCE_PHOTO,
                                    raw_input=described or None, image_path=image_path)
    else:
        if not described:
            return ToolResult(summary="Нечего записывать: нет ни фото, ни описания.", ok=False)
        estimate = safe_estimate_from_text(described, context=context)
        meal = service.create_draft(ctx.db, ctx.subject.id, estimate,
                                    source=SOURCE_TEXT, raw_input=described)

    bus.publish(MEAL_LOGGED, {"meal_id": meal.id, "user_id": ctx.subject.id})

    # Всё, из чего сложилась цифра, — в ответ инструмента: иначе агент пересказывает
    # одно число и человеку нечего поправлять.
    details = [f"Записал черновиком: {meal.title} — примерно {meal.kcal} ккал "
               f"(Б {meal.protein} / Ж {meal.fat} / У {meal.carbs})."]
    if estimate.portion:
        details.append(f"Считал так: {estimate.portion}.")
    if estimate.components:
        details.append("Из чего сложилось: " + ", ".join(estimate.components) + ".")
    if estimate.note:
        details.append(estimate.note.rstrip(".") + ".")
    if estimate.confidence == "low":
        details.append("Уверенности мало, стоит посмотреть цифры.")
    details.append(f"Это оценка, ждёт подтверждения (meal_id={meal.id}).")
    if estimate.question:
        details.append(f"Спроси у человека: {estimate.question}")

    return ToolResult(
        summary=" ".join(details),
        data={"meal_id": meal.id, "kcal": meal.kcal, "confidence": estimate.confidence,
              "question": estimate.question},
        card=_meal_card(meal, subtitle=estimate.portion),
    )


@tool(
    name="confirm_meal",
    module=MODULE,
    title="Подтвердить запись",
    description="""
    Подтвердить черновик приёма пищи или поправить его. Передавай только те поля,
    которые человек назвал; остальные останутся как есть.
    Если человек уточнил вес или способ приготовления (а не сами цифры) — передай
    weight_g и cooking, и оценка пересчитается сама. Не выдумывай калории вместо него.
    Если человек назвал цифры — передай их, запись пометится как скорректированная вручную.
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
            "weight_g": {"type": "number",
                         "description": "Вес порции в граммах, если человек уточнил его — "
                                        "цифры пересчитаются"},
            "cooking": {"type": "string",
                        "description": "Способ приготовления, если человек уточнил его — "
                                       "цифры пересчитаются"},
        },
        "required": ["meal_id"],
    },
    # Этот инструмент сам по себе и есть подтверждение: человек только что сказал
    # «да» или назвал верную цифру. Спрашивать разрешения на его «да» — абсурд.
    auto_from=0,
)
def confirm_meal(ctx: ToolContext, meal_id: int, kcal: int = None, protein: int = None,
                 fat: int = None, carbs: int = None, title: str = None,
                 weight_g: float = None, cooking: str = None) -> ToolResult:
    corrections = {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs, "title": title}
    recounted = None

    # Человек ответил на вопрос про вес или готовку, а цифр не назвал — значит их
    # надо пересчитать. Иначе ответ «было 400 грамм» ничего не меняет, и вопрос,
    # который ассистент только что задал, оказывается пустой вежливостью.
    if (weight_g or cooking) and kcal is None:
        draft = service.get_meal(ctx.db, ctx.subject.id, meal_id)
        if draft is None:
            return ToolResult(summary="Такой записи нет.", ok=False)
        recounted = safe_estimate_from_text(
            _describe(draft.raw_input or draft.title, weight_g, cooking),
            context=_person_context(ctx),
        )
        if recounted.kcal:
            corrections.update(kcal=recounted.kcal, protein=recounted.protein,
                               fat=recounted.fat, carbs=recounted.carbs)

    meal = service.confirm_meal(ctx.db, ctx.subject.id, meal_id,
                                {k: v for k, v in corrections.items() if v is not None})
    if meal is None:
        return ToolResult(summary="Такой записи нет.", ok=False)

    bus.publish(MEAL_CONFIRMED, {"meal_id": meal.id, "user_id": ctx.subject.id})

    summary = (f"Записал: {meal.title} — {meal.kcal} ккал "
               f"(Б {meal.protein} / Ж {meal.fat} / У {meal.carbs}), {meal.status_label}.")
    if recounted is not None:
        summary += f" Пересчитал с уточнением: {recounted.portion or 'по новым данным'}."
    return ToolResult(summary=summary, data={"meal_id": meal.id}, card=_meal_card(meal))


@tool(
    name="delete_meal",
    module=MODULE,
    title="Удалить запись о еде",
    description="""
    Удалить ошибочную запись о еде: человек записал не то, продиктовал дважды или
    передумал. Всегда указывай meal_id, если он звучал в разговоре: без него
    удаляется самая свежая запись, а она может оказаться не той, о которой речь.
    Не путай с исправлением: если человек уточняет блюдо или вес, это confirm_meal.
    """,
    parameters={
        "type": "object",
        "properties": {
            "meal_id": {"type": "integer",
                        "description": "Номер записи; без него — самая свежая"},
        },
    },
    # Удаление необратимо, поэтому на всех уровнях, кроме максимального, агент
    # готовит действие и ждёт «да». Ровно то, ради чего заведены pending-действия.
    auto_from=3,
)
def delete_meal(ctx: ToolContext, meal_id: int = None) -> ToolResult:
    meal = (service.get_meal(ctx.db, ctx.subject.id, meal_id) if meal_id
            else service.last_meal(ctx.db, ctx.subject.id))
    if meal is None:
        return ToolResult(summary="Такой записи о еде нет — возможно, её уже удалили.", ok=False)

    title, kcal = meal.title, meal.kcal
    service.delete_meal(ctx.db, ctx.subject.id, meal.id)
    return ToolResult(
        summary=f"Удалил запись: {title} — {kcal} ккал. В дневном балансе она больше не считается.",
        data={"meal_id": meal.id},
    )


@tool(
    name="delete_activity",
    module=MODULE,
    title="Удалить запись об активности",
    description="""
    Удалить ошибочную запись об активности. activity_id — из предыдущего ответа;
    без него удаляется самая свежая запись.
    """,
    parameters={
        "type": "object",
        "properties": {
            "activity_id": {"type": "integer",
                            "description": "Номер записи; без него — самая свежая"},
        },
    },
    auto_from=3,
)
def delete_activity(ctx: ToolContext, activity_id: int = None) -> ToolResult:
    entry = (service.get_activity(ctx.db, ctx.subject.id, activity_id) if activity_id
             else service.last_activity(ctx.db, ctx.subject.id))
    if entry is None:
        return ToolResult(summary="Такой записи об активности нет.", ok=False)

    label = ACTIVITY_LABELS.get(entry.kind, entry.kind).lower()
    unit, value, kcal = ACTIVITY_UNITS.get(entry.kind, ""), int(entry.value), entry.kcal
    service.delete_activity(ctx.db, ctx.subject.id, entry.id)
    return ToolResult(
        summary=f"Удалил запись: {label} — {value} {unit}, примерно {kcal} ккал.",
        data={"activity_id": entry.id},
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
    notes = _known(ctx, limit=10)

    prompt = (
        f"Человек: {ctx.subject.display_name}. Цель: {profile.goal_label}. "
        f"Суточная норма: {profile.daily_kcal} ккал.\n"
        f"Что ел за последние дни: {', '.join(history) if history else 'записей пока нет'}.\n"
        f"Что известно с досок {ctx.actor.display_name}: "
        f"{'; '.join(notes) if notes else 'ничего особенного'}."
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
