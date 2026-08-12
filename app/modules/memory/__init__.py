"""Memory module — what the assistant remembers about each family member.

Always on: an assistant that cannot remember anything is not an assistant. It has
no toggle on the onboarding matrix, but its data is still strictly personal
(scoped by user_id) — one person's notes never reach another's context.
"""
from app.core.module import Module, NavItem
from app.modules.memory import models, tools  # noqa: F401  (регистрирует таблицы и инструменты)
from app.modules.memory.routes import reminders_router, router

module = Module(
    name="memory",
    title="Память и заметки",
    description="Ассистент помнит предпочтения, ограничения и напоминания",
    routers=[router, reminders_router],
    nav_items=[NavItem(slug="memory", label="Память и заметки", url="/memory",
                       icon="note", short="Память"),
               NavItem(slug="reminders", label="Напоминания", url="/reminders",
                       icon="clock", short="Напомнить")],
    per_user=True,
    always_on=True,
)
