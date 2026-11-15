"""aiogram wiring: turns Telegram updates into calls to bot.replies."""

import logging
import re

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from asgiref.sync import sync_to_async

from . import replies

log = logging.getLogger(__name__)

# /command -> the word the shared grammar expects. Anything not listed is
# passed through unchanged, so a new tracker "water" works as /water.
ALIASES = {"drink": "alcohol", "drinks": "alcohol", "bed": "slept", "sleep": "slept", "wake": "woke"}
COMMANDS = [
    ("coffee", "Add a coffee"), ("drink", "Add a drink"), ("junk", "Add junk food"),
    ("spent", "Expense: /spent 12.5 food lunch"), ("gym", "Workout: /gym legs 60"),
    ("slept", "Going to bed (or /slept 23:30)"), ("woke", "Woke up (or /woke 7:10)"),
    ("screen", "Screen time: /screen 3h20"), ("mood", "Mood 1–5"), ("energy", "Energy 1–5"),
    ("habits", "Check off habits"), ("today", "Today so far"), ("week", "Last 7 days"),
    ("review", "This week's review"), ("ask", "Ask about your data"), ("undo", "Undo the last entry"), ("menu", "One-tap buttons"), ("help", "What I understand"),
]


class OwnerOnly(BaseMiddleware):
    """Single-user bot: updates from anyone else are dropped without a reply."""

    def __init__(self, user_id: int):
        self.user_id = user_id

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id != self.user_id:
            log.warning("Ignored update from user %s", getattr(user, "id", None))
            return None
        return await handler(event, data)


def _markup(reply: replies.Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in reply.buttons
    ])


async def _call(fn, *args) -> replies.Reply:
    return await sync_to_async(fn)(*args)


async def _send(message: Message, reply: replies.Reply, quote: bool = False):
    if quote:
        return await message.reply(reply.text, reply_markup=_markup(reply))
    return await message.answer(reply.text, reply_markup=_markup(reply))


def build_router() -> Router:
    router = Router()

    @router.message(Command("start", "help"))
    async def help_(message: Message):
        await message.answer(replies.HELP)

    @router.message(Command("menu", "log"))
    async def menu(message: Message):
        await _send(message, await _call(replies.menu))

    @router.message(Command("today"))
    async def today(message: Message):
        await _send(message, await _call(replies.today))

    @router.message(Command("week"))
    async def week(message: Message):
        await _send(message, await _call(replies.week))

    @router.message(Command("habits"))
    async def habits(message: Message):
        await _send(message, await _call(replies.habits))

    @router.message(Command("review"))
    async def review(message: Message):
        await message.bot.send_chat_action(message.chat.id, "typing")
        await _send(message, await _call(replies.review))

    @router.message(Command("ask"))
    async def ask(message: Message, command: CommandObject):
        await message.bot.send_chat_action(message.chat.id, "typing")
        await _send(message, await _call(replies.ask, command.args or ""))

    @router.message(Command("undo"))
    async def undo(message: Message):
        await _send(message, await _call(replies.undo_last))

    @router.message(Command(re.compile(r".+")))  # any other /command goes to the grammar
    async def any_command(message: Message, command: CommandObject):
        word = ALIASES.get(command.command.lower(), command.command.lower())
        await _send(message, await _call(replies.command, word, command.args or ""))

    @router.message(F.text)
    async def free_text(message: Message):
        await message.bot.send_chat_action(message.chat.id, "typing")  # Claude may take a second
        await _send(message, await _call(replies.preview, message.text), quote=True)

    @router.callback_query(F.data.startswith("q:"))
    async def quick(callback: CallbackQuery):
        reply = await _call(replies.log, callback.data[2:])
        await callback.answer()
        await _send(callback.message, reply)

    @router.callback_query(F.data.startswith("undo:"))
    async def undo_button(callback: CallbackQuery):
        reply = await _call(replies.undo, int(callback.data.split(":", 1)[1]))
        await callback.answer()
        await callback.message.edit_text(reply.text, reply_markup=None)

    @router.callback_query(F.data.startswith("habit:"))
    async def habit(callback: CallbackQuery):
        reply = await _call(replies.toggle_habit, callback.data.split(":", 1)[1])
        await callback.answer()
        await callback.message.edit_text(reply.text, reply_markup=_markup(reply))

    @router.callback_query(F.data.startswith("ok:"))
    async def confirm(callback: CallbackQuery):
        reply = await _call(replies.confirm, int(callback.data.split(":", 1)[1]))
        await callback.answer()
        await callback.message.edit_text(reply.text, reply_markup=_markup(reply))

    @router.callback_query(F.data.startswith("no:"))
    async def cancel(callback: CallbackQuery):
        reply = await _call(replies.cancel, int(callback.data.split(":", 1)[1]))
        await callback.answer()
        await callback.message.edit_text(reply.text, reply_markup=None)

    @router.callback_query(F.data.in_({"habits", "today", "menu"}))
    async def shortcut(callback: CallbackQuery):
        reply = await _call(getattr(replies, callback.data))
        await callback.answer()
        await _send(callback.message, reply)

    return router


def build_dispatcher(owner_id: int) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(OwnerOnly(owner_id))
    dispatcher.include_router(build_router())
    return dispatcher
