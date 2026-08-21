"""Голос персоны для служебных реплик.

Характер ассистента живёт в основном цикле разговора, а служебные пути — сводки,
напоминания, аварийные ответы — исторически говорили строками из кода, и
заведённая человеку личность на этих шагах пропадала (прогон 78 — ровно этот
класс поломки на подтверждениях). Здесь — общий способ произнести уже
случившийся факт голосом ассистента конкретного человека.

Это не `system_prompt()`: та функция собирает промпт для полноценного хода —
с инструментами, историей, памятками. Здесь решать нечего, кроме как сказать
пару фраз характером, — лишний контекст только рискует утащить модель в сторону.

Контракт с честностью: факты сюда приходят уже посчитанными кодом, работа модели
— только слова (тот же принцип, что у BOARD_STATS_SYSTEM). Любой сбой — модель
недоступна, отвечает заглушка, ответ пуст — возвращает `fallback`: каноническую
строку, которой этот путь говорил всегда. Поэтому сценарии стенда и офлайн-режим
живут как жили: без модели человек слышит прежние слова.
"""
from app.agent.llm import ROUTINE, LLMClient, LLMUnavailable, client as default_client
from app.agent.prompts import TONE, WHO, character_block
from app.core import instructions
from app.core.logging import get_logger
from app.core.models import User

logger = get_logger("voice")


def speak(subject: User, hint: str, fallback: str = "", llm: LLMClient = None) -> str:
    """Произнести факт голосом ассистента этого человека — или `fallback`.

    Заглушка (`LLM_STUB`) говорить характером не умеет — её разбор ключевых слов
    на такую просьбу ответил бы «пока могу немногое», и это уехало бы человеку
    вместо сводки. Поэтому со включённой заглушкой сразу отдаётся `fallback`.
    """
    llm = llm or default_client
    if getattr(getattr(llm, "cfg", None), "stub", False):
        return fallback
    system = "\n".join(part for part in
                       (WHO, character_block(instructions.character(subject)), TONE)
                       if part)
    try:
        response = llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": hint}],
            task=ROUTINE,
        )
    except LLMUnavailable:
        return fallback
    except Exception as error:
        # Служебная реплика — не место ронять сводку или ответ целиком: любой
        # сбой здесь стоит человеку только манеры, а не смысла.
        logger.warning(f"Голос персоны не сложился ({error!r}) — говорю канонической строкой")
        return fallback
    text = (getattr(response, "content", "") or "").strip()
    return text or fallback
