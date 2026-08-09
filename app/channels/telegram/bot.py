"""Telegram-бот — основной канал общения с семьёй.

    python -m app.channels.telegram.bot

The bot is a channel, not a second brain: every message goes through the same
`Agent.respond` as the web panel and lands in the same `chat_messages`. A person
sends a photo of their plate here and sees the estimate in the panel, and vice
versa.

It also listens to the Event Bus, so an anomaly at the gate or a due reminder
reaches people's phones without anyone opening anything.
"""
import asyncio
import json
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from app.agent.runtime import agent, approve_action, reject_action
from app.core.config import settings
from app.core.db import session_scope
from app.core.events import AGENT_MESSAGE, bus
from app.core.logging import get_logger
from app.core.models import User
from app.modules import load_modules

logger = get_logger("telegram")

WELCOME = (
    "Здравствуйте! Я семейный ассистент.\n\n"
    "Пришлите фото тарелки — прикину, что и сколько. Напишите словами, что съели, — "
    "запишу. Спросите про дом — расскажу, что видели камеры.\n\n"
    "Чтобы я узнал вас, отправьте код из панели: /start ваш_код"
)

NOT_LINKED = (
    "Я вас пока не знаю. Попросите главу семьи открыть «Семья и модули» → «Добавить участника» "
    "и передать вам короткий код, затем отправьте: /start код"
)


def _find_user(db, telegram_id: str) -> Optional[User]:
    return db.query(User).filter(User.telegram_id == str(telegram_id)).one_or_none()


def _link_by_code(db, telegram_id: str, code: str) -> Optional[User]:
    user = db.query(User).filter(User.link_code == code.strip()).one_or_none()
    if user is None:
        return None
    user.telegram_id = str(telegram_id)
    user.link_code = None      # код одноразовый
    db.commit()
    return user


def _confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, сделай", callback_data=f"approve:{pending_id}"),
        InlineKeyboardButton(text="Не надо", callback_data=f"reject:{pending_id}"),
    ]])


def _meal_keyboard(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Всё верно", callback_data=f"meal_ok:{meal_id}"),
        InlineKeyboardButton(text="Поправить",
                             url=f"{settings.public_base_url}/nutrition/meal?meal_id={meal_id}&state=estimate"),
    ]])


def _keyboard_for(reply) -> Optional[InlineKeyboardMarkup]:
    """Turn the agent's cards into inline buttons — same actions as in the panel."""
    for card in reply.cards:
        if card.get("type") == "confirm":
            return _confirm_keyboard(card["pending_id"])
        if card.get("type") == "meal" and card.get("is_estimate"):
            return _meal_keyboard(card["meal_id"])
    return None


dispatcher = Dispatcher()


@dispatcher.message(CommandStart(deep_link=False))
async def on_start(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    code = parts[1].strip() if len(parts) > 1 else ""

    with session_scope() as db:
        user = _find_user(db, message.from_user.id)
        if user is not None:
            await message.answer(f"С возвращением, {user.display_name}. Что записываем?")
            return
        if code:
            user = _link_by_code(db, message.from_user.id, code)
            if user is not None:
                await message.answer(f"Узнал вас, {user.display_name}. Теперь можно просто писать мне.")
                return
            await message.answer("Такой код не подошёл. Попросите новый в панели.")
            return
    await message.answer(WELCOME)


@dispatcher.message(Command("help"))
async def on_help(message: Message):
    await message.answer(
        "Что я умею:\n"
        "• фото тарелки → оценка КБЖУ и запись\n"
        "• «съел суп и салат» → то же самое словами\n"
        "• «сколько сегодня» → баланс дня\n"
        "• «что было ночью» → события с камер\n"
        "• «запомни, что…» → положу в память\n\n"
        f"Панель со статистикой: {settings.public_base_url}"
    )


@dispatcher.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    with session_scope() as db:
        user = _find_user(db, message.from_user.id)
        if user is None:
            await message.answer(NOT_LINKED)
            return
        user_id = user.id

    file = await bot.get_file(message.photo[-1].file_id)
    buffer = await bot.download_file(file.file_path)
    image = buffer.read()

    await _respond(message, user_id, message.caption or "", image=image)


@dispatcher.message(F.text)
async def on_text(message: Message):
    with session_scope() as db:
        user = _find_user(db, message.from_user.id)
        if user is None:
            await message.answer(NOT_LINKED)
            return
        user_id = user.id

    await _respond(message, user_id, message.text)


async def _respond(message: Message, user_id: int, text: str, image: bytes = None):
    await message.bot.send_chat_action(message.chat.id, "typing")
    # Агентский цикл синхронный (БД + HTTP к модели) — уводим его из event loop.
    reply = await asyncio.to_thread(_run_agent, user_id, text, image)
    await message.answer(reply["text"], reply_markup=reply["keyboard"])


def _run_agent(user_id: int, text: str, image: bytes = None) -> dict:
    with session_scope() as db:
        user = db.get(User, user_id)
        reply = agent.respond(db, user, text, image=image, channel="telegram")
        return {"text": reply.text, "keyboard": _keyboard_for(reply)}


@dispatcher.callback_query(F.data.startswith(("approve:", "reject:", "meal_ok:")))
async def on_callback(query: CallbackQuery):
    action, _, raw_id = query.data.partition(":")
    try:
        target_id = int(raw_id)
    except ValueError:
        await query.answer()
        return

    result = await asyncio.to_thread(_resolve_action, query.from_user.id, action, target_id)
    await query.answer()
    if result:
        await query.message.answer(result)
        await query.message.edit_reply_markup(reply_markup=None)


def _resolve_action(telegram_id: int, action: str, target_id: int) -> str:
    with session_scope() as db:
        user = _find_user(db, telegram_id)
        if user is None:
            return NOT_LINKED

        if action == "approve":
            return approve_action(db, target_id, user, channel="telegram").summary
        if action == "reject":
            return reject_action(db, target_id, user).summary

        from app.agent.runtime import run_tool_directly
        return run_tool_directly(db, user, "confirm_meal", {"meal_id": target_id},
                                 mode="telegram").summary


# --- события шины → сообщения в Telegram ---------------------------------

class Notifier:
    """Delivers Event Bus messages to people's phones.

    Lives in the bot process and is subscribed once at startup, so anything that
    publishes AGENT_MESSAGE — a camera anomaly, a due reminder, a scheduled digest —
    reaches the family without knowing Telegram exists.
    """

    def __init__(self, bot: Bot, loop: asyncio.AbstractEventLoop):
        self.bot = bot
        self.loop = loop

    def handle(self, payload: dict):
        user_ids = payload.get("user_ids") or []
        text = payload.get("text") or ""
        severity = payload.get("severity", "info")
        if not user_ids or not text:
            return

        with session_scope() as db:
            chats = [
                u.telegram_id for u in db.query(User).filter(User.id.in_(user_ids))
                if u.telegram_id
            ]

        prefix = {"alarm": "⚠️ ", "attention": "🔎 "}.get(severity, "")
        keyboard = None
        if payload.get("event_id"):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text="Смотреть запись",
                url=f"{settings.public_base_url}/security/events?event_id={payload['event_id']}",
            )]])

        for chat_id in chats:
            asyncio.run_coroutine_threadsafe(
                self._send(chat_id, prefix + text, keyboard), self.loop
            )

    async def _send(self, chat_id: str, text: str, keyboard):
        try:
            await self.bot.send_message(chat_id, text, reply_markup=keyboard)
        except Exception:
            logger.exception(f"Не смог отправить сообщение в чат {chat_id}")


async def main():
    if not settings.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан — боту нечем представиться")

    load_modules()
    bus.start()

    bot = Bot(token=settings.telegram_token)
    bus.subscribe(AGENT_MESSAGE, Notifier(bot, asyncio.get_running_loop()).handle)

    logger.info("Telegram-бот запущен")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
