"""Agent tools for the nutrition module.

`log_meal` never writes a final record: it produces a draft with an explicit
estimate, and `confirm_meal` is what turns it into a confirmed (or hand-corrected)
entry. That two-step shape is a product requirement — the number came from a model
looking at a plate, and the person must be able to fix it in one gesture.
"""
from app.agent.llm import PLANNING, LLMUnavailable, client as llm_client
from app.agent.prompts import MEAL_PLAN_SYSTEM
from app.agent.registry import ALWAYS_ASK, ToolContext, ToolResult, tool
from app.core import instructions
from app.core.events import ACTIVITY_LOGGED, MEAL_CONFIRMED, MEAL_LOGGED, bus
from app.core.logging import get_logger
from app.core.websearch import SearchUnavailable
from app.modules.memory import knowledge
from app.modules.nutrition import lookup, service
from app.modules.nutrition.models import (
    ACTIVITY_KCAL, ACTIVITY_LABELS, ACTIVITY_UNITS, SOURCE_PHOTO, SOURCE_TEXT,
)
from app.modules.nutrition.vision import (
    MealEstimate, estimate_from_image, safe_estimate_from_text,
)

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

    Памятка про еду — оттуда же, откуда профиль: это человек про себя, а не про
    разговор. Здесь она и нужна больше всего — оценка тарелки у того, кто без
    желчного, и у того, кто набирает вес, разная не цифрами, а тем, о чём
    ассистент переспросит и что предложит дальше.
    """
    profile = service.get_profile(ctx.db, ctx.subject.id)
    notes = _known(ctx)
    lines = [
        "Что известно об этом человеке (учитывай, но не упоминай в ответе):",
        f"- цель: {profile.goal_label}, суточная норма {profile.daily_kcal} ккал",
    ]
    if profile.weight_kg:
        lines.append(f"- вес: {profile.weight_kg:g} кг")
    memo = instructions.memo(ctx.db, ctx.subject.id, MODULE)
    if memo:
        lines.append(f"- сам просил учитывать: {memo}")
    if notes:
        lines.append(f"- с досок {ctx.actor.display_name}: " + "; ".join(notes))
    return "\n".join(lines)


def _known(ctx: ToolContext, limit: int = 8) -> list:
    """Факты с досок собеседника — короткими строками для промпта.

    Чьи это доски, в промпте сказано прямо: профиль тут одного человека, а
    знания другого, когда глава семьи считает еду за ребёнка (ADR-0005).
    """
    return knowledge.person_facts(ctx.db, ctx.actor.id, limit=limit)


def _refined_by_web(estimate: MealEstimate, again) -> MealEstimate:
    """Пересчитать оценку по составу товара, если это фабричная еда.

    Первый проход отвечает на вопрос «что это»; узнал марку — второй считает уже
    по этикетке, а не на глаз. Оценка «батончика Марс» по внешнему виду и его же
    состав с сайта производителя расходятся в разы, и второй проход стоит одного
    лишнего обращения к модели.

    Всё здесь необязательно: поиск не настроен, товар не нашёлся, модель не
    ответила — остаётся обычная оценка, а еда всё равно записана.
    """
    if not estimate.brand:
        return estimate

    facts = lookup.safe_lookup(estimate.brand)
    if facts is None:
        return estimate

    try:
        refined = again(facts.as_prompt())
    except LLMUnavailable:
        logger.warning("Справка нашлась, но пересчитать по ней не вышло — оставляю первую оценку")
        return estimate

    if not refined.kcal:                       # пустой пересчёт хуже первой оценки
        return estimate

    refined.brand = estimate.brand
    refined.sources = facts.sources
    if facts.domains:
        source_note = "Состав взят с " + ", ".join(facts.domains)
        refined.note = f"{refined.note.rstrip('.')}. {source_note}" if refined.note else source_note
        refined.note = refined.note[:255]
    return refined


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
        def by_photo(facts: str = None) -> MealEstimate:
            return estimate_from_image(image, hint=described or None, context=context, facts=facts)

        try:
            estimate = by_photo()
        except LLMUnavailable:
            return ToolResult(
                summary="Не смог разглядеть блюдо — модель не отвечает. Опишите словами, я запишу.",
                ok=False,
            )
        # Товар видно на фото — считаем по его составу, а не по виду упаковки.
        estimate = _refined_by_web(estimate, by_photo)
        image_path = service.save_image(image, ctx.subject.id)
        meal = service.create_draft(ctx.db, ctx.subject.id, estimate, source=SOURCE_PHOTO,
                                    raw_input=described or None, image_path=image_path)
    else:
        if not described:
            return ToolResult(summary="Нечего записывать: нет ни фото, ни описания.", ok=False)
        estimate = _refined_by_web(
            safe_estimate_from_text(described, context=context),
            lambda facts: safe_estimate_from_text(described, context=context, facts=facts),
        )
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
              "question": estimate.question, "sources": estimate.sources},
        card=_meal_card(meal, subtitle=estimate.portion),
    )


@tool(
    name="confirm_meal",
    module=MODULE,
    title="Подтвердить запись",
    description="""
    Подтвердить черновик приёма пищи или поправить его. Передавай только те поля,
    которые человек назвал; остальные останутся как есть.
    meal_id не обязателен: без него правится самая свежая запись — а это почти
    всегда та, о которой идёт речь. Номер нужен, только когда он звучал в разговоре
    и человек возвращается к записи постарше.
    Если человек уточнил вес или способ приготовления (а не сами цифры) — передай
    weight_g и cooking, и оценка пересчитается сама. Не выдумывай калории вместо него.
    Если человек назвал цифры — передай их, запись пометится как скорректированная вручную.
    """,
    parameters={
        "type": "object",
        "properties": {
            "meal_id": {"type": "integer",
                        "description": "Номер записи; без него — самая свежая"},
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
    },
    # Этот инструмент сам по себе и есть подтверждение: человек только что сказал
    # «да» или назвал верную цифру. Спрашивать разрешения на его «да» — абсурд.
    auto_from=0,
)
def confirm_meal(ctx: ToolContext, meal_id: int = None, kcal: int = None, protein: int = None,
                 fat: int = None, carbs: int = None, title: str = None,
                 weight_g: float = None, cooking: str = None) -> ToolResult:
    corrections = {"kcal": kcal, "protein": protein, "fat": fat, "carbs": carbs, "title": title}
    recounted = None

    # Номер записи модель знать не обязана: в историю разговора едет только текст
    # реплик, а `meal_id` живёт в ответе инструмента и до следующего хода не
    # доживает. Требовать его — значит требовать невозможного, и на поправку
    # «пицца была 20 см» ассистенту остаётся выдумывать цифры. Поэтому без номера
    # правится самая свежая запись — как в delete_meal.
    draft = (service.get_meal(ctx.db, ctx.subject.id, meal_id) if meal_id
             else service.last_meal(ctx.db, ctx.subject.id))
    if draft is None:
        return ToolResult(summary="Такой записи нет.", ok=False)
    meal_id = draft.id

    # Человек ответил на вопрос про вес или готовку, а цифр не назвал — значит их
    # надо пересчитать. Иначе ответ «было 400 грамм» ничего не меняет, и вопрос,
    # который ассистент только что задал, оказывается пустой вежливостью.
    if (weight_g or cooking) and kcal is None:
        described = _describe(draft.raw_input or draft.title, weight_g, cooking)
        context = _person_context(ctx)
        recounted = _refined_by_web(
            safe_estimate_from_text(described, context=context),
            lambda facts: safe_estimate_from_text(described, context=context, facts=facts),
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
    name="lookup_product",
    module=MODULE,
    title="Найти товар в интернете",
    description="""
    Найти в интернете калорийность и БЖУ фабричного товара: батончика, газировки,
    пиццы из доставки, готового блюда из сети. Нужен, когда человек спрашивает про
    товар, но ничего не ел: «сколько калорий в банке колы?», «а в пепперони из Додо?».
    Название пиши так, как товар ищут: марка, название, размер или вес упаковки —
    «пицца Пепперони Додо 25 см», а не «пицца».
    Если человек это съел, вызывай log_meal: он сам сходит за составом и запишет.
    Если он поправляет уже записанное («пицца была 20 см») — это confirm_meal:
    там состав тоже ищется, но цифры доедут до записи, а не останутся в ответе.
    Ответ — цифры с этикетки; назови их вместе с тем, где они нашлись.
    """,
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "Марка и название товара, при известном — вес или размер"},
        },
        "required": ["name"],
    },
    read_only=True,
)
def lookup_product(ctx: ToolContext, name: str) -> ToolResult:
    if not lookup.available():
        return ToolResult(
            summary="Поиск в интернете не настроен, поэтому точного состава я не найду. "
                    "Могу прикинуть на глаз, если опишете товар.",
            ok=False,
        )

    try:
        facts = lookup.lookup(name)
    except SearchUnavailable:
        return ToolResult(summary=f"Не нашёл в интернете состав «{name}».", ok=False)
    except LLMUnavailable:
        return ToolResult(summary="Нашёл страницы, но разобрать их сейчас нечем — модель молчит.",
                          ok=False)

    if not facts.known:
        return ToolResult(summary=f"Про «{name}» нашлось, но без цифр состава.", ok=False)

    return ToolResult(
        summary=facts.summary(),
        data={
            "title": facts.title,
            "per_100g": vars(facts.per_100g) if facts.per_100g else None,
            "per_portion": vars(facts.per_portion) if facts.per_portion else None,
            "portion": facts.portion,
            "sources": facts.sources,
            "confidence": facts.confidence,
        },
    )


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
    name="clear_nutrition_period",
    module=MODULE,
    title="Убрать записи о еде за период",
    description="""
    Убрать разом все записи за период: «удали статистику за неделю», «сотри
    сегодняшнюю еду», «почисти месяц». Записи исчезают насовсем вместе со снимками
    тарелок, цифры пересчитываются.
    period: day — сегодняшний день, week — последние семь дней вместе с сегодняшним,
    month — последние 30 дней. Других периодов инструмент не умеет: если человек
    просит убрать «за вчера», «за март» или «с 1 по 5 число», не подставляй ближайший
    период — скажи, что такие дни убираются по одному на экране «Статистика питания»,
    там у каждого дня своя кнопка.
    what: meals — только еда, activity — только активность, all — и то и другое.
    Одну ошибочную запись убирает delete_meal, а не этот инструмент.
    """,
    parameters={
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["day", "week", "month"],
                       "description": "Сегодня, последние семь дней или последние 30 дней"},
            "what": {"type": "string", "enum": ["meals", "activity", "all"],
                     "description": "Что именно убрать; по умолчанию и еду, и активность"},
        },
        "required": ["period"],
    },
    # Одно «да» стирает здесь месяц истории, и восстановить её неоткуда: спрашиваем
    # всегда, даже у того, кто разрешил агенту всё остальное.
    auto_from=ALWAYS_ASK,
)
def clear_nutrition_period(ctx: ToolContext, period: str, what: str = service.WHAT_ALL) -> ToolResult:
    if period not in service.PERIODS:
        return ToolResult(summary=f"Не знаю такого периода: {period}. Есть день, неделя и месяц.",
                          ok=False)
    if what not in service.WHAT_LABELS:
        what = service.WHAT_ALL

    window = service.PERIOD_WINDOWS[period]
    removed = service.clear_period(ctx.db, ctx.subject.id, period, what)
    if not removed:
        return ToolResult(summary=f"Убирать было нечего: {service.WHAT_LABELS[what]} {window} "
                                  f"и так не заведены.",
                          data={"meals": 0, "activity": 0})

    return ToolResult(
        summary=f"Убрал {window}: {removed.words}. Записей больше нет, дневной баланс "
                f"и статистика пересчитаны.",
        data={"meals": removed.meals, "activity": removed.activity},
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
    memo = instructions.memo(ctx.db, ctx.subject.id, MODULE)

    prompt = (
        f"Человек: {ctx.subject.display_name}. Цель: {profile.goal_label}. "
        f"Суточная норма: {profile.daily_kcal} ккал.\n"
        f"Что ел за последние дни: {', '.join(history) if history else 'записей пока нет'}.\n"
        f"Что известно с досок {ctx.actor.display_name}: "
        f"{'; '.join(notes) if notes else 'ничего особенного'}."
    )
    # Идеи — то место, где памятка весит больше всего: гастрит и «без острого»
    # меняют не цифру в плане, а сам список блюд.
    if memo:
        prompt += f"\nЧто человек просил учитывать: {memo}"

    try:
        # Единственная задача ассистента, где модели есть что обдумывать: уложиться
        # в норму, свести цель, обойти то, что человеку нельзя, и не повторить
        # вчерашнее — всё сразу. Отсюда и `PLANNING` вместо общей ручки чата.
        raw = llm_client.json_completion(MEAL_PLAN_SYSTEM, prompt, max_tokens=900, task=PLANNING)
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
