"""The bot is driven end to end through aiogram with a fake Telegram
session, so no network or token is needed."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from asgiref.sync import sync_to_async

from bot import replies
from bot.handlers import build_dispatcher
from tracking import days, defaults, logbook, metrics
from tracking.logbook import TrackerLog
from tracking.models import AppSettings, Entry, Expense, Operation, SleepSession, Tracker, Workout

OWNER, STRANGER = 4242, 666
CHAT = Chat(id=OWNER, type="private")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            reply_to = None
            if method.reply_parameters:
                reply_to = Message(message_id=method.reply_parameters.message_id, date=datetime.now(UTC), chat=CHAT,
                                   text=self.incoming.get(method.reply_parameters.message_id))
            return Message(message_id=1000 + len(self.calls), date=datetime.now(UTC), chat=CHAT, text=method.text,
                           reply_markup=method.reply_markup, reply_to_message=reply_to)
        return True

    incoming: dict = {}

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""


class Chatter:
    """Sends messages and presses buttons as a given user."""

    def __init__(self):
        self.session = FakeSession()
        self.session.incoming = {}
        self.bot = Bot("4242:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dispatcher = build_dispatcher(OWNER)
        self.n = 0

    async def say(self, text, user=OWNER):
        self.n += 1
        self.session.incoming[self.n] = text
        message = Message(message_id=self.n, date=datetime.now(UTC), chat=CHAT, text=text,
                          from_user=User(id=user, is_bot=False, first_name="U"))
        before = len(self.session.calls)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.n, message=message))
        return self.session.calls[before:]

    async def press(self, data, on: Message, user=OWNER):
        self.n += 1
        callback = CallbackQuery(id=str(self.n), from_user=User(id=user, is_bot=False, first_name="U"),
                                 chat_instance="x", data=data, message=on)
        before = len(self.session.calls)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.n, callback_query=callback))
        return self.session.calls[before:]

    async def sent(self, text):
        """Say something and return the bot's message as Telegram would."""
        calls = await self.say(text)
        send = next(c for c in calls if isinstance(c, SendMessage))
        reply_to = None
        if send.reply_parameters:
            reply_to = Message(message_id=send.reply_parameters.message_id, date=datetime.now(UTC), chat=CHAT, text=text)
        return Message(message_id=900 + self.n, date=datetime.now(UTC), chat=CHAT, text=send.text,
                       reply_markup=send.reply_markup, reply_to_message=reply_to)


def buttons(message_or_call):
    markup = message_or_call.reply_markup
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


@pytest.fixture
def chat(transactional_db):
    defaults.install()
    s = AppSettings.load()
    s.timezone = "America/Toronto"
    s.save()
    return Chatter()


pytestmark = pytest.mark.django_db(transaction=True)
value = sync_to_async(metrics.value)
count = sync_to_async(lambda model: model.objects.count())


async def test_strangers_are_ignored(chat):
    assert await chat.say("/coffee", user=STRANGER) == []
    assert await chat.say("coffee", user=STRANGER) == []
    assert await chat.press("q:coffee", on=Message(message_id=1, date=datetime.now(UTC), chat=CHAT), user=STRANGER) == []
    assert await count(Entry) == 0


async def test_quick_commands_log_immediately_with_an_undo_button(chat):
    today = await sync_to_async(days.today)()
    reply = await chat.sent("/coffee")
    assert reply.text == "✓ Coffee +1 · 1 today"
    await chat.say("/coffee 2")
    assert await value("tracker:coffee", today) == 3

    [undo] = buttons(reply)
    calls = await chat.press(undo, on=reply)
    assert any(isinstance(c, AnswerCallbackQuery) for c in calls)
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.text.startswith("↩ Undone") and edit.reply_markup is None
    assert await value("tracker:coffee", today) == 2


@pytest.mark.parametrize(
    "text,metric,expected",
    [
        ("/drink", "tracker:alcohol", 1),
        ("/junk 2", "tracker:junk", 2),
        ("/spent 12.5 food lunch", "expenses.total", Decimal("12.50")),
        ("/gym legs 60", "exercise.minutes", 60),
        ("/screen 3h20", "tracker:screen", 200),
        ("/mood 4", "tracker:mood", 4),
        ("/energy 2", "tracker:energy", 2),
        ("/read", "tracker:read", 1),
        ("/COFFEE", "tracker:coffee", 1),
    ],
)
async def test_each_command(chat, text, metric, expected):
    reply = await chat.sent(text)
    assert reply.text.startswith("✓"), reply.text
    assert await value(metric, await sync_to_async(days.today)()) == expected
    op = await sync_to_async(Operation.objects.get)()
    assert op.source == "telegram"


async def test_slept_and_woke(chat):
    assert "Bedtime" in (await chat.sent("/slept")).text
    assert "Woke up" in (await chat.sent("/woke")).text
    session = await sync_to_async(SleepSession.objects.get)()
    assert session.bedtime and session.wake_time


async def test_a_new_tracker_is_a_command_without_code(chat):
    await sync_to_async(Tracker.objects.create)(slug="water", name="Water", kind="counter")
    assert (await chat.sent("/water")).text == "✓ Water +1 · 1 today"


async def test_mood_without_a_value_offers_buttons(chat):
    reply = await chat.sent("/mood")
    assert buttons(reply) == [f"q:mood {n}" for n in range(1, 6)]
    calls = await chat.press("q:mood 5", on=reply)
    assert next(c for c in calls if isinstance(c, SendMessage)).text == "✓ Mood: 5"
    assert await value("tracker:mood", await sync_to_async(days.today)()) == 5


async def test_mistakes_are_explained_and_save_nothing(chat):
    for text, fragment in [("/mood 9", "at most 5"), ("/spent 12 unicorns", "Unknown category"),
                           ("/blargh", "understand"), ("/screen", "How long")]:
        reply = await chat.sent(text)
        assert fragment in reply.text, reply.text
        assert buttons(reply) == []
    assert await count(Entry) == 0 and await count(Expense) == 0


async def test_free_text_asks_before_saving(chat):
    reply = await chat.sent("2 coffees, spent 12.5 food milk, legs 60")
    assert reply.text == "Save this?\n+ Coffee +2\n+ $12.50 · Groceries · milk\n+ Workout: Legs · 1h"
    assert buttons(reply) == ["ok", "no"]
    assert await count(Entry) == 0  # nothing yet

    calls = await chat.press("ok", on=reply)
    edit = next(c for c in calls if isinstance(c, EditMessageText))
    assert edit.text.startswith("✓ Coffee +2 · 2 today") and buttons(edit)[0].startswith("undo:")
    assert await count(Entry) == 1 and await count(Expense) == 1 and await count(Workout) == 1


async def test_free_text_can_be_cancelled_or_rejected(chat):
    reply = await chat.sent("coffee")
    calls = await chat.press("no", on=reply)
    assert "Nothing was saved" in next(c for c in calls if isinstance(c, EditMessageText)).text
    assert await count(Entry) == 0
    assert "understand" in (await chat.sent("what a day")).text
    assert "at most 5" in (await chat.sent("mood 8")).text


async def test_undo_command(chat):
    assert (await chat.sent("/undo")).text == "Nothing to undo."
    await chat.say("/coffee")
    await chat.say("/drink")
    assert "Undone" in (await chat.sent("/undo")).text
    today = await sync_to_async(days.today)()
    assert await value("tracker:alcohol", today) is None and await value("tracker:coffee", today) == 1


async def test_habit_buttons_toggle(chat):
    today = await sync_to_async(days.today)()
    reply = await chat.sent("/habits")
    assert "▫️ Read 20 min" in reply.text and "habit:read" in buttons(reply)
    calls = await chat.press("habit:read", on=reply)
    assert "✅ Read 20 min" in next(c for c in calls if isinstance(c, EditMessageText)).text
    assert await value("tracker:read", today) == 1
    calls = await chat.press("habit:read", on=reply)
    assert "✖️ Read 20 min" in next(c for c in calls if isinstance(c, EditMessageText)).text
    assert await value("tracker:read", today) == 0


async def test_menu_help_and_shortcuts(chat):
    menu = await chat.sent("/menu")
    data = buttons(menu)
    assert {"q:coffee", "q:alcohol", "q:junk", "q:slept", "q:woke", "q:mood 3", "habits", "today"} <= set(data)
    assert all(len(d.encode()) <= 64 for d in data)  # Telegram's callback data limit
    assert "/spent 12.5 food lunch" in (await chat.sent("/start")).text
    calls = await chat.press("today", on=menu)
    assert "Not logged yet" in next(c for c in calls if isinstance(c, SendMessage)).text


async def test_today_and_week_summaries(chat):
    await chat.say("/coffee 3")
    await chat.say("/drink 0")
    await chat.say("/mood 4")
    await chat.say("/read")
    today = (await chat.sent("/today")).text
    assert "Coffee: <b>3</b>" in today and "Alcohol: <b>0</b>" in today and "Mood: <b>4</b>" in today
    assert "Habits: <b>1 of 4</b> done" in today
    assert "❗ At most 2 coffees: 3 / 2" in today
    unlogged = today.split("Not logged yet")[1]
    assert "Junk food" in unlogged and "Alcohol" not in unlogged and "Meditate" in unlogged

    week = (await chat.sent("/week")).text
    assert "Coffee: <b>3</b> avg over 1 day" in week
    assert "Junk food: not logged" in week
    assert "Read 20 min: <b>1 of 1</b> logged days" in week
    assert "vs prior week" not in week  # no earlier data to compare with


def test_week_compares_with_the_prior_week(db):
    defaults.install()
    today = days.today()
    for offset, cups in [(0, 4), (1, 4), (7, 2), (8, 2), (14, 1)]:
        logbook.apply([TrackerLog(tracker="coffee", value=cups, day=today - timedelta(days=offset))],
                      source="web")
    assert "Coffee: <b>4</b> avg over 2 days (▲100% vs prior week)" in replies.week().text


def test_user_text_is_escaped(db):
    defaults.install()
    assert "&lt;b&gt;" in replies.log("coffee <b>x</b>").text
    assert "&lt;script&gt;" in replies.preview("<script> blah").text
