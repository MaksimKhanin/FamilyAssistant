"""Nutrition module — приём пищи, активность, баланс дня, идеи питания.

Personal data end to end: every table is scoped by `user_id`, and the web layer
refuses to show one family member's figures to another (see core.auth.can_see_figures).
"""
from app.core.module import Module, NavItem
from app.modules.nutrition import models, tools  # noqa: F401  (регистрирует таблицы и инструменты)
from app.modules.nutrition.routes import router

module = Module(
    name="nutrition",
    title="Питание",
    description="Записывает еду по фото или словам, считает баланс дня",
    routers=[router],
    nav_items=[
        NavItem(slug="meal", label="Приём пищи", url="/nutrition/meal", icon="plus", group="Питание"),
        NavItem(slug="stats", label="Статистика", url="/nutrition/stats", icon="chart", group="Питание"),
        NavItem(slug="activity", label="Активность", url="/nutrition/activity", icon="pulse", group="Питание"),
        NavItem(slug="plan", label="План питания", url="/nutrition/plan", icon="book", group="Питание"),
    ],
    per_user=True,
)
