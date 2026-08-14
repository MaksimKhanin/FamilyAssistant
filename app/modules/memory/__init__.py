"""Memory module — what the assistant remembers about each family member.

Always on: an assistant that cannot remember anything is not an assistant. It has
no toggle on the onboarding matrix, but its data is still strictly personal —
sections and boards are scoped by user_id, and someone else's never reach the
assistant's context (ADR-0005).
"""
from app.core.module import Module, NavItem
from app.modules.memory import models, screens, tools  # noqa: F401  (регистрирует таблицы и инструменты)
from app.modules.memory.routes import reminders_router, router, stats_router

module = Module(
    name="memory",
    title="Знания",
    description="Ассистент помнит предпочтения, ограничения и напоминания",
    routers=[router, reminders_router, stats_router],
    nav_items=[NavItem(slug="memory", label="Знания", url="/memory",
                       icon="note", short="Знания"),
               NavItem(slug="reminders", label="Напоминания", url="/reminders",
                       icon="clock", short="Напомнить")],
    memo_hint=("Что учитывать, когда речь о ваших знаниях и напоминаниях: как вести "
               "доски, о чём напоминать заранее, что записывать самому, а что не "
               "трогать без спроса."),
    # Табло человек заводит сам, поэтому его пункта в коде нет (тикет #32).
    nav_items_for=screens.nav_items,
    per_user=True,
    always_on=True,
)
