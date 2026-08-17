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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.clock import local_now

OFFLINE_NOTE = "Это офлайн-режим без модели: понимаю только простые фразы."

#: Ключевые слова → инструмент. Порядок важен: первое совпадение выигрывает.
#: Таблица называется по тому, чем она является, — «правило» теперь занято
#: уговором человека с ассистентом, и путать их в одном файле незачем.
#:
#: «напомни» здесь тоже есть — это запасной путь на случай, когда в реплике нет
#: понятного времени (см. `_pick_tool`): «напомни, что я тебе говорила» это не
#: срочное напоминание, а факт для памяти.
KEYWORDS = [
    # Уговор — раньше запоминания: «с этого момента запомни, что…» — это правило,
    # а не факт о человеке.
    ("set_rule", ("с этого момента", "с этих пор", "заведи правило", "договоримся",
                  "впредь", "всегда записывай")),
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


_WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
             "пятниц": 4, "суббот": 5, "воскресень": 6}
#: «в 21:00» / «в 9» / «в 9 утра» / «в 9 вечера» — ровно то, что говорят вслух
#: про напоминание. Час с минутами — первая группа, голый час со словом времени
#: суток — вторая: без минут «в 9 вечера» тоже законное напоминание.
_TIME_RE = re.compile(r"\bв\s*(\d{1,2})[:.](\d{2})\b|\bв\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?(?:\s|$)")


def _parse_reminder_time(text: str, now: datetime) -> Optional[datetime]:
    """«Завтра в 9 утра» → конкретный момент, или None, если время не названо.

    Тот же смысл, что и у set_reminder для настоящей модели («вычисли дату и
    время из слов человека»), но по трём словам, а не по пониманию: без явного
    часа не гадаем — это не напоминание, а обычный факт для `remember`.
    """
    lowered = text.lower()
    day = now
    if "послезавтра" in lowered:
        day = now + timedelta(days=2)
    elif "завтра" in lowered:
        day = now + timedelta(days=1)
    elif "сегодня" not in lowered:
        for word, weekday in _WEEKDAYS.items():
            if word in lowered:
                ahead = (weekday - now.weekday()) % 7
                day = now + timedelta(days=ahead or 7)
                break

    match = _TIME_RE.search(lowered)
    if not match:
        return None
    if match.group(1):
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        hour, minute = int(match.group(3)), 0
        if match.group(4) in ("вечера", "ночи") and hour < 12:
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


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
    # «Напомни» с понятным временем — это set_reminder, а не заметка на память
    # (см. описание инструмента remember: «для напоминаний со сроком —
    # set_reminder»). Без времени в реплике гадать не о чем — тогда это уже
    # обычная просьба запомнить, и решает общая таблица ниже.
    if "set_reminder" in available and "напомни" in lowered:
        if _parse_reminder_time(text, local_now()) is not None:
            return "set_reminder"
    for name, keywords in KEYWORDS:
        if name in available and any(word in lowered for word in keywords):
            return name
    return None


def _arguments_for(name: str, text: str) -> Dict[str, Any]:
    if name == "set_rule":
        cleaned = re.sub(r"^\s*(с этого момента|с этих пор|заведи правило|договоримся|впредь)[,:]?\s*",
                         "", text, flags=re.I)
        return {"text": cleaned.strip() or text.strip()}

    if name == "remember":
        cleaned = re.sub(r"^\s*(запомни|запомнить|не забудь|напомни)[,:]?\s*", "", text, flags=re.I)
        return {"text": cleaned.strip() or text.strip()}

    if name == "set_reminder":
        when = _parse_reminder_time(text, local_now())
        cleaned = re.sub(r"^\s*напомни( мне)?[,:]?\s*", "", text, flags=re.I)
        cleaned = re.sub(r"\b(послезавтра|завтра|сегодня)\b", "", cleaned, flags=re.I)
        for word in _WEEKDAYS:
            cleaned = re.sub(rf"\b{word}\w*\b", "", cleaned, flags=re.I)
        cleaned = _TIME_RE.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,:")
        return {"text": cleaned or text.strip(), "at": when.strftime("%Y-%m-%d %H:%M")}

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


#: Число в записи доски — «вода 456», «02:50 170». Первое найденное и берём:
#: у настоящей модели `raw` в ответе честнее, у заглушки этого различения нет.
_EVENT_VALUE_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _board_event_guess(text: str) -> dict:
    """Наивный разбор записи доски по её словарю типов (`BOARD_EVENTS_SYSTEM`).

    Без этой ветки заглушка отвечала общей заметкой без «events» вовсе —
    `extract_events` не отличает «заглушка не умеет разобрать» от «величин
    правда нет», и статистика любой доски в обычном офлайн-режиме молча
    показывала бы ноль всегда. Первое число из текста записи и тип из словаря —
    грубо, но с честной низкой уверенностью, как и остальные ветки этой
    заглушки; тип берётся тот, чьё имя названо в самой записи («кофе 2 чашки» —
    «кофе», а не первый по списку), и только если ни один не назван — первый
    из словаря, как раньше (UX-находка: плашка уточнения предлагает угаданный
    тип первым, и «первый по словарю» почти всегда был не тем).
    """
    dictionary, _, entry = text.partition("Запись:\n")
    kinds = [line.strip()[2:].split(" (", 1)[0].strip()
            for line in dictionary.splitlines() if line.strip().startswith("- ")]
    kinds = [k for k in kinds if k]
    if not kinds:
        return {"events": []}
    lowered = entry.lower()
    kind = next((k for k in kinds if k.lower() in lowered), kinds[0])
    match = _EVENT_VALUE_RE.search(entry)
    if not match:
        return {"events": []}
    value = float(match.group().replace(",", "."))
    return {"events": [{"kind": kind, "value": value, "confidence": "low",
                        "raw": entry.strip()[:120]}]}


def json_completion(system: str, user_content) -> dict:
    """Подставные ответы там, где код ждёт JSON: оценка блюда, план, разбор события."""
    text = user_content if isinstance(user_content, str) else " ".join(
        part.get("text", "") for part in user_content if isinstance(part, dict)
    )

    if "величины по словарю типов этой доски" in system:
        return _board_event_guess(text)

    if "блюд" in system and "фотограф" in system:
        return {"title": "Блюдо с фото", "kcal": 420, "protein": 18, "fat": 16, "carbs": 48,
                "portion": "тарелка", "confidence": "low",
                "note": "Офлайн-режим: цифры условные, поправьте вручную"}

    if "съеденное" in system:
        # `estimate_from_text` склеивает описание еды и то, что «известно об
        # этом человеке» (цель, норма, памятки — см. `_person_context`), одной
        # строкой через пустую строку между ними. Модели этот блок помечен
        # «учитывай, но не упоминай в ответе»; заглушке эту границу нужно
        # уважать явно, иначе служебный контекст обрежется прямо в название
        # блюда, которое человек видит в списке съеденного.
        food_text = text.split("\n\n", 1)[0].strip()
        return {"title": (food_text[:60] or "Приём пищи"), "kcal": 350, "protein": 14,
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
                "message": "Кто-то у камеры в необычное время. Это оценка сита, а не факт."}

    return {"note": OFFLINE_NOTE}
