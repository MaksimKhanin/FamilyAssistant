"""Офлайн-режим агента для пробного запуска — без модели и без ключей.

Нужен ровно для одного: чтобы локальный запуск (`python run_local.py`) можно было
пощупать целиком — чат, выбор инструмента, трейс, карточка, подтверждение — не
заводя API-ключ. Никакого «интеллекта» здесь нет: это разбор ключевых слов,
который решает, какой инструмент дёрнуть.

Включается сам, когда не задан `LLM_MODEL`/`LLM_BASE_URL`, либо явно через
`LLM_STUB=1`. В ответах честно помечается, что это офлайн-режим — чтобы никто не
принял заглушку за работающую модель.
"""
import re
from typing import Any, Dict, List, Optional

OFFLINE_NOTE = "Это офлайн-режим без модели: понимаю только простые фразы."

#: Ключевые слова → инструмент. Порядок важен: первое совпадение выигрывает.
RULES = [
    ("remember", ("запомни", "запомнить", "не забудь", "напомни")),
    # Придумать еду — раньше, чем её записать: «придумай что-нибудь на ужин» иначе
    # уедет в log_meal по слову «на ужин». Рецепт — ещё раньше: «рецепт борща» уже
    # не вопрос «что поесть».
    ("dish_recipe", ("рецепт", "как готовить", "как приготовить", "распиши")),
    ("suggest_dish", ("поесть", "съесть", "что приготовить", "придумай", "предложи",
                      "поужинать", "позавтракать", "перекусить", "идеи", "план")),
    ("log_meal", ("съел", "съела", "поел", "поела", "выпил", "выпила", "перекусил", "на завтрак",
                  "на обед", "на ужин")),
    ("log_activity", ("шаг", "прошёл", "прошел", "тренировк", "пробежал", "велосипед", "прогулял")),
    ("get_nutrition_stats", ("сколько сегодня", "статистик", "баланс", "калори", "норм")),
    ("recall", ("что ты помнишь", "что помнишь", "вспомни", "из памяти", "память")),
    # Уборка идёт раньше показа: «пометь просмотренными события» тоже содержит «событ».
    ("mark_events_seen", ("просмотрен", "прочитан", "я всё видел", "я все видел",
                          "убери уведомл", "не маяч", "погаси значок")),
    ("clear_archive", ("почисти архив", "очисти архив", "удали стар", "убери стар",
                       "удали запис", "освободи мест")),
    ("get_security_log", ("дом", "камер", "ночью", "событ", "тревог", "калитк")),
]

ACTIVITY_KINDS = [("шаг", "steps"), ("тренировк", "workout"), ("велосипед", "bike"), ("прогул", "walk")]

#: Сроки, которые человек называет словами, а не цифрой: «всё старше двух дней».
WORD_DAYS = [("месяц", 30), ("недел", 7), ("трёх дн", 3), ("трех дн", 3),
             ("двух дн", 2), ("двух сут", 2), ("сутк", 1), ("вчера", 1)]


def _days_from(text: str, default: int) -> int:
    numbers = re.findall(r"\d+", text)
    if numbers:
        return int(numbers[0])
    lowered = text.lower()
    return next((days for word, days in WORD_DAYS if word in lowered), default)


def _last_user_text(messages: List[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):   # мультимодальное сообщение
            return " ".join(part.get("text", "") for part in content if part.get("type") == "text")
    return ""


def _pick_tool(text: str, available: set) -> Optional[str]:
    lowered = text.lower()
    for name, keywords in RULES:
        if name in available and any(word in lowered for word in keywords):
            return name
    return None


def _arguments_for(name: str, text: str) -> Dict[str, Any]:
    if name == "remember":
        cleaned = re.sub(r"^\s*(запомни|запомнить|не забудь|напомни)[,:]?\s*", "", text, flags=re.I)
        return {"text": cleaned.strip() or text.strip()}

    if name == "log_meal":
        return {"text": text.strip()}

    if name == "log_activity":
        lowered = text.lower()
        kind = next((k for word, k in ACTIVITY_KINDS if word in lowered), "steps")
        numbers = re.findall(r"\d+", text)
        return {"kind": kind, "value": float(numbers[0]) if numbers else 30}

    if name == "get_nutrition_stats":
        lowered = text.lower()
        period = "week" if "недел" in lowered else "month" if "месяц" in lowered else "day"
        return {"period": period}

    if name == "recall":
        # Голое «что ты помнишь?» превращается в пустой запрос — recall на него
        # отвечает свежими записями, а не поиском фразы-триггера.
        cleaned = re.sub(r"^\s*(что ты помнишь|что помнишь|вспомни)( про| о| об)?[,:]?\s*",
                         "", text, flags=re.I).strip(" ?")
        return {"query": cleaned}

    if name == "get_security_log":
        lowered = text.lower()
        period = "week" if "недел" in lowered else "today"
        return {"period": period, "only": "anomaly" if "тревог" in lowered else "all"}

    if name == "mark_events_seen":
        # Без срока — «всё просмотрено»: так эту фразу и произносят.
        return {"older_than_days": _days_from(text, 0)}

    if name == "clear_archive":
        return {"older_than_days": _days_from(text, 7)}

    if name == "suggest_dish":
        lowered = text.lower()
        slot = next((s for s in ("завтрак", "обед", "ужин", "перекус") if s in lowered), None)
        return {"wish": text.strip(), **({"slot": slot} if slot else {})}

    if name == "dish_recipe":
        cleaned = re.sub(r"^\s*(расскажи |напиши |распиши )?(рецепт|как готовить|как приготовить)"
                         r"( блюда| для)?[,:]?\s*", "", text, flags=re.I).strip(" ?")
        return {"name": cleaned or text.strip()}

    return {}


def chat(messages: List[dict], tools: Optional[List[dict]] = None):
    """Тот же контракт, что у `LLMClient.chat`, но без модели."""
    from app.agent.llm import LLMResponse, ToolCall

    # Инструмент уже отработал — остаётся пересказать результат человеку.
    if messages and messages[-1].get("role") == "tool":
        summary = (messages[-1].get("content") or "").strip()
        return LLMResponse(content=summary or "Готово.")

    available = {t["function"]["name"] for t in (tools or [])}
    text = _last_user_text(messages)
    name = _pick_tool(text, available)

    if name is None:
        return LLMResponse(content=(
            "Пока могу немногое: скажите, что съели, спросите про дом или статистику, "
            f"попросите что-нибудь запомнить. {OFFLINE_NOTE}"
        ))

    return LLMResponse(tool_calls=[ToolCall(id=f"stub_{name}", name=name,
                                            arguments=_arguments_for(name, text))])


def json_completion(system: str, user_content) -> dict:
    """Подставные ответы там, где код ждёт JSON: оценка блюда, план, разбор события."""
    text = user_content if isinstance(user_content, str) else " ".join(
        part.get("text", "") for part in user_content if isinstance(part, dict)
    )

    if "блюд" in system and "фотограф" in system:
        return {"title": "Блюдо с фото", "kcal": 420, "protein": 18, "fat": 16, "carbs": 48,
                "portion": "тарелка", "confidence": "low",
                "note": "Офлайн-режим: цифры условные, поправьте вручную"}

    if "съеденное" in system:
        return {"title": (text.strip()[:60] or "Приём пищи"), "kcal": 350, "protein": 14,
                "fat": 12, "carbs": 42, "portion": "обычная порция", "confidence": "low",
                "note": "Офлайн-режим: цифры условные, поправьте вручную"}

    if "одно блюдо" in system:
        return {"title": "Омлет с овощами", "slot": "ужин", "kcal": 420, "protein": 24,
                "fat": 26, "carbs": 14, "portion": "порция ~250 г",
                "why": f"Простое и быстрое. {OFFLINE_NOTE}", "question": ""}

    if "рецепт одного блюда" in system:
        return {"title": (text.split(".")[0].replace("Блюдо:", "").strip() or "Блюдо"),
                "portions": 2, "kcal": 420, "protein": 24, "fat": 26, "carbs": 14,
                "ingredients": ["яйца — 4 шт.", "овощи — 200 г", "масло — 1 ст. л."],
                "steps": ["Нарезать овощи.", "Обжарить их пару минут.",
                          "Залить взбитыми яйцами и довести под крышкой."],
                "note": OFFLINE_NOTE}

    if "рацион на несколько дней" in system:
        return {
            "days": [
                {"title": "Завтра", "kcal": 2050, "meals": [
                    {"name": "Овсянка с ягодами", "slot": "завтрак", "kcal": 380},
                    {"name": "Суп и салат", "slot": "обед", "kcal": 620},
                    {"name": "Рыба с овощами", "slot": "ужин", "kcal": 550},
                ]},
                {"title": "Послезавтра", "kcal": 2000, "meals": [
                    {"name": "Творог с мёдом", "slot": "завтрак", "kcal": 340},
                    {"name": "Гречка с курицей", "slot": "обед", "kcal": 640},
                    {"name": "Омлет с зеленью", "slot": "ужин", "kcal": 480},
                ]},
            ],
            "comment": f"Это идеи, а не предписание. {OFFLINE_NOTE}",
        }

    if "обычную жизнь дома" in system:
        return {"verdict": "check", "reason": "Офлайн-режим: разобрать кадр некому",
                "message": "Кто-то у камеры в необычное время. Это оценка правил, а не факт."}

    return {"note": OFFLINE_NOTE}
