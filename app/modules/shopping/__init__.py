"""Модуль «Покупки» — общий список покупок семьи.

Family-shared data (scoped by family_id): список один на дом, как камеры, — кто
бы ни положил в него молоко, купит его тот, кто окажется в магазине. Пишут в
него словами через ассистента («добавь молоко и хлеб в покупки») и руками на
экране; вычёркивают так же.

Шов с питанием намеренно тонкий: расписанный рецепт или план — это текст с
ингредиентами, и модель передаёт их в add_to_shopping_list обычным списком.
Отдельного «собери корзину из плана» инструмента нет, пока не понадобится.
"""
from app.core.module import Module, NavItem
from app.modules.shopping import models, tools  # noqa: F401  (регистрирует таблицы и инструменты)
from app.modules.shopping.routes import router as ui_router

module = Module(
    name="shopping",
    title="Покупки",
    description="Общий список покупок: положить словами, вычеркнуть в магазине",
    routers=[ui_router],
    nav_items=[NavItem(slug="shopping", label="Покупки", url="/shopping",
                       icon="cart", short="Покупки")],
    memo_hint=("Что учитывать в покупках: где закупаетесь, что берёте всегда, "
               "какие марки не брать."),
    per_user=False,
)
