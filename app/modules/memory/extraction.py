"""Разбор записи доски на величины (тикет #30, спека #19).

Это тот же приём, что и оценка блюда по описанию: модель отдаёт JSON, а код
превращает его в строки базы. Клиент модели внедряется параметром — в тестах
сюда приходит подставной, и настоящая модель не вызывается никогда.

Разбор происходит один раз, при написании записи, а не при сборке сводки: иначе
цифра за прошлый вторник менялась бы просто оттого, что сегодня модель прочла
лог иначе (ADR-0002). Числа отсюда потом складывает код, а не модель.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from app.agent.llm import ESTIMATE, LLMClient, LLMUnavailable, client as default_client
from app.agent.prompts import BOARD_EVENTS_SYSTEM
from app.core.clock import to_local, to_utc, utc_now
from app.core.logging import get_logger

logger = get_logger("memory.extraction")

#: Уверенность разбора. Низкая в сумму не идёт, пока человек не уточнил.
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
CONFIDENCE = (LOW, MEDIUM, HIGH)

#: Одна запись — не отчёт: столько величин из неё хватит любому логу.
MAX_EVENTS = 20
RAW_LIMIT = 255
UNIT_LIMIT = 16


@dataclass
class ExtractedEvent:
    """Величина, вынутая из записи: тип, время, число и единица."""
    kind: str
    at: datetime          # наивный UTC, как всё в базе
    value: float
    unit: Optional[str] = None
    confidence: str = LOW
    raw: Optional[str] = None

    @property
    def certain(self) -> bool:
        return self.confidence != LOW


#: Дата обязана идти вместе со временем: голая «2026-08-12» — это день, а не
#: момент, и полночь из него выдумывать не надо (как в разборе напоминаний).
_DATE_WITH_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _moment(raw, fallback: datetime) -> datetime:
    """Локальное «ГГГГ-ММ-ДД ЧЧ:ММ» от модели → наивный UTC.

    Не разобралось — берём время самой записи: у величины всегда есть момент,
    и лучше момент записи, чем выдуманный.
    """
    if not raw or not _DATE_WITH_TIME.match(str(raw).strip()):
        return fallback
    try:
        value = datetime.fromisoformat(str(raw).strip().replace(" ", "T"))
    except ValueError:
        return fallback
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(value)


def _coerce(raw: dict, types: dict, fallback_at: datetime, text: str) -> Optional[ExtractedEvent]:
    """Одна величина из ответа модели — или None, если это не величина.

    Числа без числа не бывает: без разбираемого `value` строку заводить не за чем.
    """
    kind = str(raw.get("kind") or "").strip()[:64]
    if not kind:
        return None
    try:
        value = float(raw.get("value"))
    except (TypeError, ValueError):
        return None

    confidence = str(raw.get("confidence") or "").lower()
    if confidence not in CONFIDENCE:
        # Неизвестное слово вместо уверенности — не повод пускать число в сумму.
        confidence = LOW

    known = types.get(kind.lower())
    if known is None:
        # Тип берётся из словаря доски: чужое имя — повод переспросить человека,
        # а не заводить «кормление», «еду» и «молоко» на одной доске вперемешку.
        confidence = LOW
    else:
        kind = known.name

    if value < 0:
        # Съеденное и потраченное — разные типы, а не число со знаком: минус
        # значит, что модель не нашла подходящего типа.
        value = abs(value)
        confidence = LOW

    # Единица словаря — канонична по написанию: «мл» и «Мл» не должны разъехаться
    # в две величины. Но названная в записи другая единица сохраняется как есть —
    # пересчитывать литры в миллилитры не дело разбора, а суммы всё равно идут
    # по типу вместе с единицей.
    known_unit = known.unit if known else None
    given = str(raw.get("unit") or "").strip()[:UNIT_LIMIT]
    unit = (known_unit if known_unit and (not given or given.lower() == known_unit.lower())
            else given or known_unit)
    fragment = str(raw.get("raw") or "").strip() or text.strip()
    return ExtractedEvent(kind=kind, at=_moment(raw.get("at"), fallback_at), value=value,
                          unit=unit or None, confidence=confidence,
                          raw=fragment[:RAW_LIMIT])


def _dictionary(types: Sequence) -> str:
    return "\n".join(f"- {t.name}" + (f" ({t.unit})" if t.unit else "") for t in types)


def extract_events(text: str, instruction: str = None, types: Sequence = (),
                   at: datetime = None, llm: LLMClient = None) -> List[ExtractedEvent]:
    """Величины одной записи по словарю типов её доски.

    `types` — строки словаря доски (name, unit); `at` — время записи, к которому
    привязываются величины без своего времени.
    """
    llm = llm or default_client
    at = at or utc_now()
    if not types or not text.strip():
        return []

    prompt = (
        f"Словарь типов этой доски:\n{_dictionary(types)}\n\n"
        + (f"Инструкция доски: {instruction}\n\n" if instruction else "")
        + f"Время записи: {to_local(at):%Y-%m-%d %H:%M}.\n\n"
        f"Запись:\n{text.strip()}"
    )
    # Вынуть из записи величину с единицей и временем — та же работа с числами,
    # что и оценка тарелки, поэтому и ручка размышления у них общая.
    raw = llm.json_completion(BOARD_EVENTS_SYSTEM, prompt, task=ESTIMATE)
    known = {t.name.lower(): t for t in types}
    parsed = [_coerce(item, known, at, text)
              for item in (raw.get("events") or [])[:MAX_EVENTS] if isinstance(item, dict)]
    return [event for event in parsed if event is not None]


def safe_extract_events(text: str, instruction: str = None, types: Sequence = (),
                        at: datetime = None, llm: LLMClient = None) -> Optional[List[ExtractedEvent]]:
    """То же, но никогда не падает.

    Запись сохраняется немедленно и независимо от разбора: потерять лог кормлений
    из-за моргнувшей модели хуже, чем остаться без цифры.

    Разбор, который не состоялся, и разбор, не нашедший величин, — разные вещи, и
    возвращаются они по-разному: `None` — «не смог», пустой список — «величин
    нет». По `None` прежние величины записи остаются на месте: молчание модели не
    повод стирать то, что человек уже уточнил.
    """
    try:
        return extract_events(text, instruction=instruction, types=types, at=at, llm=llm)
    except LLMUnavailable:
        logger.warning("Модель недоступна — запись сохранена без разбора на величины")
    except (AttributeError, TypeError, ValueError) as error:
        logger.warning(f"Не разобрал запись на величины: {error}")
    return None
