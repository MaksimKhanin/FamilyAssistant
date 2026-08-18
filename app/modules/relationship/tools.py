"""Инструмент модуля «Подход»: форсировать разбор, не дожидаясь автоматики.

Обычный ход разбора — не отсюда: за него отвечает планировщик
(app/scheduler.py, run_relationship_reviews), раз в REVIEW_EVERY сообщений.
Этот инструмент — для «разбери сейчас» по прямой просьбе человека.
Инструмента «записать заметку» в чате намеренно нет: дедуп, мерж и ротация
живут в одном месте (service.run_review), а не россыпью спонтанных вызовов
посреди разговора.
"""
from app.agent.registry import ToolContext, ToolResult, tool
from app.modules.relationship import service

MODULE = "relationship"


@tool(
    name="review_approach",
    module=MODULE,
    title="Разобрать подход сейчас",
    description="""
    Разобрать разговор прямо сейчас и обновить заметки о подходе, не дожидаясь
    автоматического разбора раз в несколько сообщений. Вызывай по прямой
    просьбе человека («разбери сейчас», «обнови заметки», «подведи итог») —
    не сам по себе и не в рамках обычного ответа.
    """,
    parameters={"type": "object", "properties": {}},
    auto_from=2,
)
def review_approach(ctx: ToolContext) -> ToolResult:
    # Доски — глазами того, кто разговаривает (ctx.actor), тем же путём, что
    # и остальные инструменты знаний (ADR-0005): «от лица» тут не годится —
    # это личный профиль конкретно говорящего, не то, что можно вести чужими
    # руками.
    updated = service.run_review(ctx.db, ctx.actor)
    if not updated:
        return ToolResult(
            summary="Разбирать пока нечего — нового разговора с прошлого раза не набралось.",
            ok=False,
        )
    return ToolResult(summary="Обновил заметки о подходе и итог разговора.")
