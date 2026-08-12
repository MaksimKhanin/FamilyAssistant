"""Прототип к тикету #13: инструкция доски — системный промпт или обогащение.

Гоняет одни и те же три вопроса по фикстуре лога питания/сна через две сборки
промпта и две редакции инструкции, повторяя каждую клетку несколько раз, и
печатает ответы рядом с эталоном (gold.md). Смотреть глазами; грубая выжимка
чисел — только чтобы заметить разброс между повторами.

Запуск из корня репозитория, с тем же .env, что и у панели:

    python -m prototype.board_instruction.run            # 2×2×3 вопроса × 2 повтора
    python -m prototype.board_instruction.run --repeats 3
    python -m prototype.board_instruction.run --cell enrichment:base   # одна клетка

Это выброска: код отвечает на вопрос дизайна и не претендует на продакшен.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

HERE = Path(__file__).parent
FIXTURE = (HERE / "fixture.txt").read_text(encoding="utf-8")

NOW = "суббота, 11.08.2026, 21:00"

#: Инструкция ровно в той редакции, какую написал бы владелец доски (base), и
#: расширенная (extended) — проверяем, лечит ли дописывание инструкции ловушку «Итого».
INSTRUCTIONS = {
    "base": "Заметки содержат лог питания и сна ребёнка. "
            "Если время не указано — бери время отправки сообщения.",
    "extended": "Заметки содержат лог питания и сна ребёнка. "
                "Если время события не указано в тексте — бери время отправки записи. "
                "Строки «Итого N мл» — это сводки, которые пишут люди, а не кормления: "
                "в суммы их не включай.",
}

QUESTIONS = [
    "Сколько ребёнок съел сегодня?",
    "Сколько ребёнок съедал в сутки в среднем за последнюю неделю?",
    "Сколько ребёнок спал сегодня?",
]

GOLD = [
    "780 мл (5 кормлений за 11.08)",
    "≈840 мл/сутки, полных данных только за 2 дня (10.08 — 900, 11.08 — 780)",
    "зафиксировано 2 ч 23 мин (07:20–07:58 и 08:20–10:05); ночной сон не логируется",
]


def build_messages(variant: str, instruction: str, question: str) -> list:
    """Две сборки: инструкция в системном промпте против обогащения контекста."""
    if variant == "system":
        system = (
            "Ты — семейный ассистент. Ты работаешь с доской заметок «Питание и сон ребёнка». "
            f"Инструкция владельца доски: {instruction} "
            f"Сейчас {NOW}. Отвечай кратко и точно, с числами."
        )
        user = f"Записи доски:\n\n{FIXTURE}\n\nВопрос: {question}"
    elif variant == "enrichment":
        system = "Ты — семейный ассистент. Отвечай кратко и точно, с числами."
        user = (
            f"Контекст для ответа — доска заметок «Питание и сон ребёнка» "
            f"(инструкция владельца доски: {instruction}):\n\n{FIXTURE}\n\n"
            f"Сейчас {NOW}.\n\nВопрос: {question}"
        )
    else:
        raise ValueError(f"неизвестная сборка: {variant}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def numbers(text: str) -> list:
    """Грубая выжимка чисел из ответа — только чтобы увидеть разброс повторов."""
    return re.findall(r"\d+(?:[.,]\d+)?", text)[:6]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--cell", default=None,
                        help="одна клетка вида сборка:инструкция, напр. enrichment:base")
    parser.add_argument("--reasoning", default=None,
                        help="перекрыть режим размышления (off/low/medium/high)")
    args = parser.parse_args()

    # Импорт здесь, а не наверху: сборку промптов можно смотреть и без
    # установленных зависимостей приложения.
    from app.agent.llm import LLMClient, LLMUnavailable

    client = LLMClient()
    if not client.configured:
        raise SystemExit("LLM не сконфигурирован: нужен .env с LLM_BASE_URL/LLM_MODEL "
                         "(тот же, что у панели).")

    cells = [(v, i) for v in ("system", "enrichment") for i in INSTRUCTIONS]
    if args.cell:
        variant, instruction = args.cell.split(":")
        cells = [(variant, instruction)]

    for q_no, question in enumerate(QUESTIONS):
        print(f"\n{'=' * 72}\nВОПРОС {q_no + 1}: {question}\nЭТАЛОН:   {GOLD[q_no]}\n{'=' * 72}")
        for variant, instruction_key in cells:
            for attempt in range(1, args.repeats + 1):
                label = f"{variant}:{instruction_key} #{attempt}"
                try:
                    response = client.chat(
                        build_messages(variant, INSTRUCTIONS[instruction_key], question),
                        temperature=0.2,
                        max_tokens=700,
                        reasoning=args.reasoning,
                    )
                    answer = response.content or "(пустой ответ)"
                except LLMUnavailable as e:
                    answer = f"(модель недоступна: {e})"
                print(f"\n--- {label}\n{answer}\n    числа: {numbers(answer)}")


if __name__ == "__main__":
    main()
