"""aiogram wiring. Each person has one menu message (bot/screens.py) that is
edited in place as they tap; typed commands move it to the bottom of the
chat. Messages worth keeping (receipts for typed entries, reviews, answers,
reminders) come from bot/replies.py as new messages."""

import logging
import re

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from asgiref.sync import sync_to_async

from tracking.models import TelegramLink
from tracking.owner import acting_as

from . import replies, screens

log = logging.getLogger(__name__)

# /command -> the word the shared grammar expects. Anything not listed is
# passed through unchanged, so a new tracker "water" works as /water.
ALIASES = {"drink": "alcohol", "drinks": "alcohol", "bed": "slept", "wake": "woke"}
# Commands that open a menu screen when sent without arguments.
SCREENS = {"start": "home", "menu": "home", "today": "home", "log": "home", "habits": "habits",
           "sleep": "sleep", "spend": "spend", "spent": "spend", "gym": "gym", "workout": "gym",
           "week": "week", "help": "help", "check": "check", "rate": "rate", "more": "more"}
COMMANDS = [
    ("menu", "Dashboard"), ("check", "Fill in what's missing today"), ("habits", "Check off habits"),
    ("sleep", "Bedtime and wake-up"), ("spend", "Log spending"), ("gym", "Log a workout"),
    ("week", "Last 7 days"), ("review", "This week's review"), ("ask", "Ask about your data"),
    ("undo", "Undo the last entry"), ("help", "What I understand"),
]
START_CODE = re.compile(r"^/start(?:@\w+)?\s+([A-Za-z0-9_-]{16,64})\s*$")
LEGACY = {"habits": "v:habits", "today": "v:home", "menu": "v:home"}  # buttons on messages sent before the menu


def _name(user: User) -> str:
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name


def _markup(reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in reply.buttons
    ])


async def _call(fn, *args):
    return await sync_to_async(fn)(*args)


# --- Menu and question state (TelegramLink fields) -------------------------------------------


def _link() -> TelegramLink:
    return TelegramLink.load()


def _remember(**fields):
    TelegramLink.objects.update(**fields)


async def _delete(bot: Bot, chat_id: int, *message_ids):
    for message_id in message_ids:
        if message_id:
            try:
                await bot.delete_message(chat_id, message_id)
            except TelegramAPIError:
                pass  # already gone, or too old to delete


async def _edit(bot: Bot, chat_id: int, message_id: int, reply) -> bool:
    try:
        await bot.edit_message_text(reply.text, chat_id=chat_id, message_id=message_id, reply_markup=_markup(reply))
    except TelegramBadRequest as e:
        return "not modified" in str(e)
    return True


async def show_menu(bot: Bot, chat_id: int, reply):
    """Send the menu as a new message at the bottom and remove the old one."""
    old = (await _call(_link)).panel_message_id
    sent = await bot.send_message(chat_id, reply.text, reply_markup=_markup(reply))
    await _call(lambda: _remember(panel_message_id=sent.message_id, panel_screen=reply.screen or "home"))
    if old != sent.message_id:
        await _delete(bot, chat_id, old)


async def refresh_menu(bot: Bot, chat_id: int, reply=None):
    """Redraw the menu where it is, e.g. after a typed entry was saved, so
    its numbers stay current. With `reply`, show that instead."""
    link = await _call(_link)
    if reply is None:
        if not link.panel_message_id:
            return
        reply = await _call(screens.render, link.panel_screen or "home")
    if link.panel_message_id and await _edit(bot, chat_id, link.panel_message_id, reply):
        await _call(lambda: _remember(panel_screen=reply.screen or "home"))
    else:
        await show_menu(bot, chat_id, reply)


async def ask_question(bot: Bot, chat_id: int, key: str, error: str = ""):
    """Send a question with a reply field; its answer is the next message."""
    q = await _call(screens.question, key)
    if q is None:
        return
    old = (await _call(_link)).prompt_message_id
    text = f"{error}\n\n{q.text}" if error else q.text
    sent = await bot.send_message(chat_id, text, reply_markup=ForceReply(input_field_placeholder=q.placeholder))
    await _call(lambda: _remember(prompt_message_id=sent.message_id, prompt=key))
    await _delete(bot, chat_id, old)


async def answer_question(message: Message, link: TelegramLink):
    bot, chat_id, key = message.bot, message.chat.id, link.prompt
    await _call(lambda: _remember(prompt_message_id=None, prompt=""))
    if key == "ask":  # the question and its answer stay in the chat
        await _delete(bot, chat_id, link.prompt_message_id)
        await bot.send_chat_action(chat_id, "typing")
        reply = await _call(replies.ask, message.text)
        await message.reply(reply.text)
        return
    reply, saved = await _call(screens.answer, key, message.text)
    await _delete(bot, chat_id, message.message_id, link.prompt_message_id)
    if saved:
        await refresh_menu(bot, chat_id, reply)
    else:
        await ask_question(bot, chat_id, key, error=reply.toast)


# --- Who is talking -------------------------------------------------------------------


class Accounts(BaseMiddleware):
    """Each update acts as the account its sender linked in Settings (see
    TelegramLink). Only private chats are served. Someone who is not linked
    can only redeem a link code; anything else gets a pointer to Settings."""

    async def __call__(self, handler, event: Update, data):
        user, chat = data.get("event_from_user"), data.get("event_chat")
        if user is None or user.is_bot or chat is None or chat.type != "private":
            return None
        message = event.message
        match = START_CODE.match(message.text or "") if message else None
        if match:
            reply, linked = await _call(replies.link, match[1], user.id, _name(user))
            await message.answer(reply.text)
            if linked is not None:
                with acting_as(linked.user):
                    await show_menu(message.bot, chat.id, await _call(screens.render, "home"))
            return None
        link = await _call(TelegramLink.for_telegram, user.id)
        if link is None:
            log.info("Update from unlinked Telegram user %s", user.id)
            if message is not None:
                await message.answer(replies.NOT_LINKED)
            return None
        with acting_as(link.user):  # sync_to_async copies the context into its thread
            return await handler(event, data)


# --- Handlers -------------------------------------------------------------------------


def build_router() -> Router:
    router = Router()

    @router.message(Command("review"))
    async def review(message: Message):
        await message.bot.send_chat_action(message.chat.id, "typing")
        await message.answer((await _call(replies.review)).text)

    @router.message(Command("ask"))
    async def ask(message: Message, command: CommandObject):
        if not (command.args or "").strip():
            await _delete(message.bot, message.chat.id, message.message_id)
            await ask_question(message.bot, message.chat.id, "ask")
            return
        await message.bot.send_chat_action(message.chat.id, "typing")
        await message.reply((await _call(replies.ask, command.args)).text)

    @router.message(Command("undo"))
    async def undo(message: Message):
        reply = await _call(screens.undo_last)
        await _delete(message.bot, message.chat.id, message.message_id)
        await show_menu(message.bot, message.chat.id, reply)

    @router.message(Command(re.compile(r".+")))  # menus, and any tracker as a command
    async def any_command(message: Message, command: CommandObject):
        word, args = command.command.lower(), (command.args or "").strip()
        if word in SCREENS and not args:
            reply = await _call(screens.render, SCREENS[word])
        else:
            reply = await _call(replies.command, ALIASES.get(word, word), args)
        await _delete(message.bot, message.chat.id, message.message_id)
        await show_menu(message.bot, message.chat.id, reply)

    @router.message(F.text)
    async def text(message: Message):
        link = await _call(_link)
        if link.prompt and link.prompt_message_id and (
            (message.reply_to_message and message.reply_to_message.message_id == link.prompt_message_id)
            or message.message_id == link.prompt_message_id + 1
        ):
            await answer_question(message, link)
            return
        await message.bot.send_chat_action(message.chat.id, "typing")  # Claude may take a second
        reply = await _call(replies.preview, message.text)
        await message.reply(reply.text, reply_markup=_markup(reply))

    @router.callback_query(F.data.regexp(r"^(v|l|u|h|q|undo|habit):") | F.data.in_(set(LEGACY)))
    async def tap(callback: CallbackQuery):
        data = LEGACY.get(callback.data, callback.data)
        kind, _, rest = data.partition(":")
        if kind == "v":
            reply = await _call(screens.render, rest)
        elif kind in ("l", "q"):
            screen, text = rest.split(":", 1) if kind == "l" else ("home", rest)
            reply = await _call(screens.log, screen, text)
        elif kind in ("u", "undo"):
            screen, _, op = rest.rpartition(":")
            reply = await _call(screens.undo, screen or "home", int(op))
        else:
            reply = await _call(screens.toggle_habit, rest)
        await callback.answer(reply.toast or None)
        bot, chat_id, message_id = callback.bot, callback.message.chat.id, callback.message.message_id
        await _edit(bot, chat_id, message_id, reply)
        old = (await _call(_link)).panel_message_id
        await _call(lambda: _remember(panel_message_id=message_id, panel_screen=reply.screen or "home"))
        if old != message_id:  # an older menu or a reminder became the menu; keep just one
            await _delete(bot, chat_id, old)

    @router.callback_query(F.data.startswith("p:"))
    async def question(callback: CallbackQuery):
        await callback.answer()
        await ask_question(callback.bot, callback.message.chat.id, callback.data[2:])

    @router.callback_query(F.data.startswith("ok:"))
    async def confirm(callback: CallbackQuery):
        reply = await _call(replies.confirm, int(callback.data[3:]))
        await callback.answer("✓ Saved" if reply.buttons else None)
        await callback.message.edit_text(reply.text, reply_markup=_markup(reply))
        await refresh_menu(callback.bot, callback.message.chat.id)

    @router.callback_query(F.data.startswith("no:"))
    async def cancel(callback: CallbackQuery):
        await _call(replies.cancel, int(callback.data[3:]))
        await callback.answer("Not saved")
        original = callback.message.reply_to_message
        await _delete(callback.bot, callback.message.chat.id, callback.message.message_id,
                      original.message_id if original else None)

    @router.callback_query(F.data.startswith("r:"))
    async def undo_receipt(callback: CallbackQuery):
        reply = await _call(replies.undo_receipt, int(callback.data[2:]), callback.message.text or "")
        await callback.answer("↩ Undone")
        await callback.message.edit_text(reply.text, reply_markup=None)
        await refresh_menu(callback.bot, callback.message.chat.id)

    @router.callback_query(F.data == "review")
    async def write_review(callback: CallbackQuery):
        await callback.answer("Writing your review…")
        await callback.bot.send_chat_action(callback.message.chat.id, "typing")
        await callback.message.answer((await _call(replies.review)).text)

    return router


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(Accounts())
    dispatcher.include_router(build_router())
    return dispatcher
