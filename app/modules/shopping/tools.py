"""Инструменты списка покупок.

Список семейный: инструменты ходят по family_id из контекста, и каждый видит и
правит один и тот же список — как события дома. Описания разграничивают
покупки и еду: «съел молоко» — это log_meal, «купи молоко» — сюда.
"""
from app.agent.registry import ToolContext, ToolResult, tool
from app.modules.shopping import service

MODULE = "shopping"


@tool(
    name="add_to_shopping_list",
    module=MODULE,
    title="Положить в покупки",
    description="""
    Добавить в общий список покупок семьи: «добавь молоко и хлеб в покупки»,
    «надо купить корм коту». Каждая позиция — отдельной строкой списка items.
    Просят купить всё для рецепта или плана — передай его ингредиенты этим же
    списком, по одному на строку, без количеств «по вкусу».
    Не путай с едой: «съел бутерброд» — это log_meal, а не покупка.
    """,
    parameters={
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"},
                      "description": "Что купить — по позиции на строку: «молоко», «хлеб»"},
        },
        "required": ["items"],
    },
    auto_from=1,
)
def add_to_shopping_list(ctx: ToolContext, items: list) -> ToolResult:
    texts = [str(item) for item in items or [] if str(item or "").strip()]
    if not texts:
        return ToolResult(summary="Пустой список — класть нечего.", ok=False)
    added, duplicates = service.add_items(ctx.db, ctx.family_id, ctx.subject.id, texts)
    if not added and duplicates:
        return ToolResult(summary="Всё это уже лежит в списке покупок.")
    words = ", ".join(item.text for item in added)
    summary = f"Положил в покупки: {words}."
    if duplicates:
        summary += " Уже лежали: " + ", ".join(duplicates) + "."
    return ToolResult(summary=summary,
                      data={"added": [item.id for item in added]})


@tool(
    name="show_shopping_list",
    module=MODULE,
    title="Показать покупки",
    description="""
    Показать общий список покупок семьи: что осталось купить и что уже
    вычеркнуто. «Что купить?», «что в списке покупок?» — это сюда.
    """,
    read_only=True,
)
def show_shopping_list(ctx: ToolContext) -> ToolResult:
    items = service.list_items(ctx.db, ctx.family_id)
    if not items:
        return ToolResult(summary="Список покупок пуст.")
    open_lines = [item.text for item in items if not item.checked]
    checked_lines = [item.text for item in items if item.checked]
    parts = []
    if open_lines:
        parts.append("Купить: " + ", ".join(open_lines) + ".")
    else:
        parts.append("Некупленного не осталось.")
    if checked_lines:
        parts.append("Уже вычеркнуто: " + ", ".join(checked_lines) + ".")
    return ToolResult(summary=" ".join(parts),
                      data={"open": len(open_lines), "checked": len(checked_lines)})


@tool(
    name="check_off_item",
    module=MODULE,
    title="Вычеркнуть покупку",
    description="""
    Вычеркнуть купленное из списка: «купил молоко», «вычеркни хлеб». Передавай
    название так, как оно лежит в списке. Если под слова подходит несколько
    позиций, инструмент откажется — уточни у человека, какую вычеркнуть.
    """,
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Что вычеркнуть — как записано в списке"},
        },
        "required": ["name"],
    },
    auto_from=2,
)
def check_off_item(ctx: ToolContext, name: str) -> ToolResult:
    item = service.check_off(ctx.db, ctx.family_id, name)
    if item is None:
        return ToolResult(
            summary=f"Не нашёл в списке одной позиции под «{name}» — переспроси у человека, "
                    f"что именно вычеркнуть.",
            ok=False,
        )
    return ToolResult(summary=f"Вычеркнул: {item.text}.", data={"item_id": item.id})
