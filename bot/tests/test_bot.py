"""The bot is driven end to end through aiogram with a fake Telegram
session, so no network or token is needed."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from asgiref.sync import sync_to_async
from django.utils import timezone

from bot import replies
from bot.handlers import build_dispatcher
from tracking import days, defaults, logbook, metrics
from tracking.logbook import TrackerLog
from tracking.models import (
    AppSettings,
    Entry,
    Expense,
    Operation,
    SleepSession,
    TelegramLink,
    Tracker,
    Workout,
)
from tracking.owner import acting_as

OWNER, STRANGER = 4242, 666
CHAT = Chat(id=OWNER, type="private")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sent = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            reply_to = None
            if method.reply_parameters:
                reply_to = Message(message_id=method.reply_parameters.message_id, date=datetime.now(UTC), chat=CHAT,
                                   text=self.incoming.get(method.reply_parameters.message_id))
            markup = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
            sent = Message(message_id=1000 + len(self.calls), date=datetime.now(UTC), chat=CHAT, text=method.text,
                           reply_markup=markup, reply_to_message=reply_to)
            self.sent.append(sent)
            return sent
        return True

    incoming: dict = {}

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""


def link(account, telegram_id=OWNER):
    """Connect `account` to a Telegram user, the way Settings → Telegram does."""
    with acting_as(account):
        code = TelegramLink.new_code()
    return TelegramLink.redeem(code, telegram_id, "U")


class Chatter:
    """Sends messages and presses buttons as a given Telegram user."""

    def __init__(self):
        self.session = FakeSession()
        self.session.incoming = {}
        self.bot = Bot("4242:TEST", session=self.session, default=DefaultBotProperties(parse_mode="HTML"))
        self.dispatcher = build_dispatcher()
        self.n = 0

    async def say(self, text, user=OWNER, chat=None, reply_to=None):
        self.n += 1
        self.session.incoming[self.n] = text
        chat = chat or (CHAT if user == OWNER else Chat(id=user, type="private"))
        message = Message(message_id=self.n, date=datetime.now(UTC), chat=chat, text=text, reply_to_message=reply_to,
                          from_user=User(id=user, is_bot=False, first_name="U", username="u"))
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
def chat(transactional_db, owner):
    defaults.install()
    s = AppSettings.load()
    s.timezone = "America/Toronto"
    s.save()
    link(owner)
    return Chatter()


pytestmark = pytest.mark.django_db(transaction=True)
value = sync_to_async(metrics.value)
count = sync_to_async(lambda model: model.objects.count())


async def test_strangers_are_only_told_how_to_connect(chat):
    for text in ("/coffee", "coffee", "/start", "/start not-a-real-code-at-all"):
        calls = await chat.say(text, user=STRANGER)
        assert len(calls) == 1 and isinstance(calls[0], SendMessage) and calls[0].chat_id == STRANGER
    assert "Settings → Telegram" in calls[0].text
    assert await chat.press("q:coffee", on=Message(message_id=1, date=datetime.now(UTC), chat=CHAT), user=STRANGER) == []
    assert await count(Entry) == 0


async def test_group_chats_are_ignored_even_for_linked_people(chat):
    assert await chat.say("/coffee", chat=Chat(id=-100, type="group")) == []
    assert await count(Entry) == 0


async def test_linking_with_a_code_from_settings(chat, owner):
    @sync_to_async
    def code():
        with acting_as(owner):
            return TelegramLink.new_code()

    raw = await code()
    calls = await chat.say(f"/start {raw}", user=STRANGER)
    assert calls[0].text.startswith("✅ Connected to <b>me</b>")
    assert calls[1].text.startswith("<b>") and "<b>Goals</b>" in calls[1].text  # the menu follows
    link = await sync_to_async(TelegramLink.for_telegram)(STRANGER)
    assert link.user == owner and link.name == "U (@u)"
    # The account has one Telegram user: the earlier one is replaced.
    assert await sync_to_async(TelegramLink.for_telegram)(OWNER) is None
    await chat.say("/coffee", user=STRANGER)
    assert await count(Entry) == 1

    # Codes work once.
    calls = await chat.say(f"/start {raw}", user=OWNER)
    assert "expired or was already used" in calls[0].text
    assert await sync_to_async(TelegramLink.for_telegram)(OWNER) is None


async def test_link_codes_expire(chat, owner):
    @sync_to_async
    def expired_code():
        with acting_as(owner):
            raw = TelegramLink.new_code()
            TelegramLink.objects.update(code_expires_at=timezone.now() - timedelta(seconds=1))
            return raw

    calls = await chat.say(f"/start {await expired_code()}", user=STRANGER)
    assert "expired" in calls[0].text
    assert await sync_to_async(TelegramLink.for_telegram)(STRANGER) is None


async def test_a_deactivated_account_stops_being_served(chat, owner):
    owner.is_active = False
    await owner.asave()
    calls = await chat.say("/coffee")
    assert "Settings → Telegram" in calls[0].text
    assert await count(Entry) == 0


def sends(calls):
    return [c for c in calls if isinstance(c, SendMessage)]


def edits(calls):
    return [c for c in calls if isinstance(c, EditMessageText)]


def deletes(calls):
    return [c.message_id for c in calls if isinstance(c, DeleteMessage)]


def toast(calls):
    return next(c.text for c in calls if isinstance(c, AnswerCallbackQuery))


async def menu(chat, command="/menu"):
    """Open the menu and return it as Telegram would (so buttons can be pressed)."""
    await chat.say(command)
    return chat.session.sent[-1]


def as_message(edit, like: Message) -> Message:
    return Message(message_id=like.message_id, date=like.date, chat=CHAT, text=edit.text, reply_markup=edit.reply_markup)


async def test_the_menu_opens_at_the_bottom_and_replaces_the_old_one(chat):
    calls = await chat.say("/menu")
    [panel] = sends(calls)
    assert "<b>Goals</b>" in panel.text and "☕ Coffee —" in panel.text
    assert deletes(calls) == [chat.n]  # the typed /menu is tidied away
    first = chat.session.sent[-1].message_id
    calls = await chat.say("/habits")
    assert sends(calls)[0].text.startswith("<b>Habits today · 0 of 4</b>")
    assert first in deletes(calls)  # only one menu at a time
    link = await sync_to_async(TelegramLink.load)()
    assert link.panel_message_id == chat.session.sent[-1].message_id and link.panel_screen == "habits"


async def test_tapping_logs_in_place_with_undo(chat):
    today = await sync_to_async(days.today)()
    panel = await menu(chat)
    calls = await chat.press("l:home:coffee", on=panel)
    assert sends(calls) == []  # nothing new in the chat
    [edit] = edits(calls)
    assert edit.message_id == panel.message_id
    assert edit.text.startswith("✓ Coffee +1 · 1 today\n\n") and "☕ Coffee <b>1</b>" in edit.text
    assert toast(calls) == "✓ Coffee +1 · 1 today"
    undo = buttons(edit)[0]
    assert undo.startswith("u:home:")
    calls = await chat.press(undo, on=as_message(edit, panel))
    assert edits(calls)[0].text.startswith("↩ Undone: coffee")
    assert await value("tracker:coffee", today) is None


async def test_screens_navigate_in_place(chat):
    panel = await menu(chat)
    for screen, heading in [("habits", "<b>Habits today"), ("sleep", "<b>Sleep</b>"), ("spend", "<b>Spending</b>"),
                            ("gym", "<b>Workouts</b>"), ("gym.legs", "<b>🦵 Legs</b>"), ("rate", "<b>How are you"),
                            ("check", "<b>Check-in · 11 left</b>"), ("more", "<b>More</b>"),
                            ("week", "<b>Last 7 days</b>"), ("help", "<b>How this works</b>"), ("home", "<b>")]:
        calls = await chat.press(f"v:{screen}", on=panel)
        [edit] = edits(calls)
        assert edit.text.startswith(heading), (screen, edit.text)
        data = buttons(edit)
        assert all(len(d.encode()) <= 64 for d in data)  # Telegram's callback data limit
        assert screen == "home" or "v:home" in data


@pytest.mark.parametrize(
    "text,metric,expected",
    [
        ("/drink", "tracker:alcohol", 1),
        ("/junk 2", "tracker:junk", 2),
        ("/spent 12.5 food lunch", "expenses.total", Decimal("12.50")),
        ("/gym legs 60", "exercise.minutes", 60),
        ("/screen 3h20", "tracker:screen", 200),
        ("/mood 4", "tracker:mood", 4),
        ("/read", "tracker:read", 1),
        ("/COFFEE", "tracker:coffee", 1),
    ],
)
async def test_each_command_logs_and_shows_the_menu(chat, text, metric, expected):
    calls = await chat.say(text)
    assert sends(calls)[0].text.startswith("✓"), sends(calls)[0].text
    assert chat.n in deletes(calls)
    assert await value(metric, await sync_to_async(days.today)()) == expected
    op = await sync_to_async(Operation.objects.get)()
    assert op.source == "telegram"


async def test_commands_without_arguments_open_their_screen(chat):
    for command, heading in [("/sleep", "<b>Sleep</b>"), ("/spend", "<b>Spending</b>"), ("/gym", "<b>Workouts</b>"),
                             ("/mood", "<b>How are you"), ("/week", "<b>Last 7 days"), ("/start", "<b>")]:
        assert sends(await chat.say(command))[0].text.startswith(heading), command
    assert await count(Entry) == 0 and await count(SleepSession) == 0


async def test_sleep_buttons(chat):
    panel = await menu(chat, "/sleep")
    assert "🌙 In bed since" in edits(await chat.press("l:sleep:slept", on=panel))[0].text
    assert "Last night:" in edits(await chat.press("l:sleep:woke", on=panel))[0].text
    session = await sync_to_async(SleepSession.objects.get)()
    assert session.bedtime and session.wake_time


async def test_a_new_tracker_works_as_a_command_and_a_button(chat):
    await sync_to_async(Tracker.objects.create)(slug="water", name="Water", kind="counter")
    assert sends(await chat.say("/water"))[0].text.startswith("✓ Water +1 · 1 today")
    panel = chat.session.sent[-1]
    assert "l:home:water" in buttons(panel)


async def test_mistakes_are_explained_and_save_nothing(chat):
    for text, fragment in [("/mood 9", "at most 5"), ("/spent 12 unicorns", "Unknown category"),
                           ("/blargh", "understand"), ("/screen", "How long")]:
        reply = sends(await chat.say(text))[0]
        assert fragment in reply.text.split("\n\n")[0], reply.text
        assert not any(d.startswith("u:") for d in buttons(reply))
    assert await count(Entry) == 0 and await count(Expense) == 0


async def test_typed_entries_are_previewed_then_kept_as_a_receipt(chat):
    panel = await menu(chat)
    reply = await chat.sent("2 coffees, spent 12.5 food milk, legs 60")
    assert reply.text == "📝 <b>Save this?</b>\n• Coffee +2\n• $12.50 · Groceries · milk\n• Workout: Legs · 1h"
    ok, no = buttons(reply)
    assert ok.startswith("ok:") and no.startswith("no:")
    assert await count(Entry) == 0  # nothing yet

    calls = await chat.press(ok, on=reply)
    receipt, refreshed = edits(calls)
    assert receipt.text.startswith("✅ <b>Saved</b>\n• Coffee +2 · 2 today") and buttons(receipt)[0].startswith("r:")
    assert refreshed.message_id == panel.message_id and "☕ Coffee <b>2</b>" in refreshed.text  # menu stays current
    assert await count(Entry) == 1 and await count(Expense) == 1 and await count(Workout) == 1

    calls = await chat.press(buttons(receipt)[0], on=as_message(receipt, reply))
    assert edits(calls)[0].text.startswith("↩ <b>Undone</b>\n<s>Coffee +2 · 2 today</s>")
    assert await count(Entry) == 0 and await count(Expense) == 0


async def test_cancelling_a_preview_removes_it(chat):
    reply = await chat.sent("coffee")
    calls = await chat.press(buttons(reply)[1], on=reply)
    assert set(deletes(calls)) == {reply.message_id, reply.reply_to_message.message_id}
    assert await count(Entry) == 0
    assert "understand" in (await chat.sent("what a day")).text
    assert "at most 5" in (await chat.sent("mood 8")).text


async def test_spending_asks_for_the_amount(chat):
    panel = await menu(chat, "/spend")
    calls = await chat.press("p:spend.food", on=panel)
    [question] = sends(calls)
    assert question.text.startswith("💸 <b>Groceries</b> · how much?") and question.reply_markup.force_reply
    asked = chat.session.sent[-1]

    calls = await chat.say("12.50 lunch", reply_to=asked)
    assert set(deletes(calls)) >= {chat.n, asked.message_id}  # question and answer are tidied away
    [edit] = edits(calls)
    assert edit.message_id == panel.message_id and edit.text.startswith("✓ $12.50 · Groceries · lunch")
    expense = await sync_to_async(Expense.objects.select_related("category").get)()
    assert expense.amount == Decimal("12.50") and expense.category.slug == "food" and expense.note == "lunch"
    link = await sync_to_async(TelegramLink.load)()
    assert link.prompt == "" and link.prompt_message_id is None


async def test_a_wrong_answer_asks_again(chat):
    panel = await menu(chat, "/gym")
    await chat.press("p:gym.legs", on=panel)
    calls = await chat.say("a while", reply_to=chat.session.sent[-1])
    [again] = sends(calls)
    assert again.text.startswith("⚠️") and "how many minutes?" in again.text and again.reply_markup.force_reply
    assert await count(Workout) == 0
    await chat.say("50", reply_to=chat.session.sent[-1])
    assert await value("exercise.minutes", await sync_to_async(days.today)()) == 50


async def test_check_in_fills_in_what_is_missing(chat):
    panel = await menu(chat, "/check")
    assert "l:check:mood 3" in buttons(panel) and "l:check:read no" in buttons(panel)
    calls = await chat.press("l:check:coffee 0", on=panel)
    edit = edits(calls)[0]
    assert edit.text.startswith("✓ Coffee = 0") and "<b>Check-in · 10 left</b>" in edit.text
    assert "l:check:coffee 0" not in buttons(edit)


async def test_undo_command(chat):
    assert sends(await chat.say("/undo"))[0].text.startswith("↩ Nothing to undo.")
    await chat.say("/coffee")
    await chat.say("/drink")
    assert sends(await chat.say("/undo"))[0].text.startswith("↩ Undone")
    today = await sync_to_async(days.today)()
    assert await value("tracker:alcohol", today) is None and await value("tracker:coffee", today) == 1


async def test_habit_buttons_toggle(chat):
    today = await sync_to_async(days.today)()
    panel = await menu(chat, "/habits")
    assert "h:read" in buttons(panel)
    calls = await chat.press("h:read", on=panel)
    assert "✅ Read 20 min" in str(buttons_labels(edits(calls)[0]))
    assert await value("tracker:read", today) == 1
    calls = await chat.press("h:read", on=panel)
    assert edits(calls)[0].text.startswith("✓ Read 20 min: not done")
    assert await value("tracker:read", today) == 0


def buttons_labels(message_or_call):
    return [b.text for row in message_or_call.reply_markup.inline_keyboard for b in row]


async def test_home_shows_today_and_goals(chat):
    await chat.say("/coffee 3")
    await chat.say("/drink 0")
    await chat.say("/mood 4")
    await chat.say("/read")
    home = sends(await chat.say("/today"))[0].text
    assert "☕ Coffee <b>3</b>" in home and "🍷 Alcohol <b>0</b>" in home and "🙂 Mood <b>4</b>" in home
    assert "✅ Habits <b>1 of 4</b>" in home
    assert "⚠️ At most 2 coffees · 3 of 2" in home
    week = sends(await chat.say("/week"))[0].text
    assert "Coffee <b>3</b> avg" in week and "Junk food —" in week and "Read 20 min <b>1 of 1</b> days" in week
    assert "▲" not in week.split("<i>")[0]  # no earlier data to compare with


async def test_buttons_on_messages_from_before_the_menu_still_work(chat):
    old = Message(message_id=5, date=datetime.now(UTC), chat=CHAT, text="old")
    calls = await chat.press("q:coffee", on=old)
    assert edits(calls)[0].text.startswith("✓ Coffee +1")
    calls = await chat.press("habits", on=old)
    assert edits(calls)[0].text.startswith("<b>Habits today")


def test_week_compares_with_the_prior_week(db):
    from bot import screens

    defaults.install()
    today = days.today()
    for offset, cups in [(0, 4), (1, 4), (7, 2), (8, 2), (14, 1)]:
        logbook.apply([TrackerLog(tracker="coffee", value=cups, day=today - timedelta(days=offset))],
                      source="web")
    assert "Coffee <b>4</b> avg ▲100%" in screens.render("week").text


def test_user_text_is_escaped(db):
    from bot import screens

    defaults.install()
    assert "&lt;b&gt;" in screens.log("home", "coffee <b>x</b>").text
    assert "&lt;script&gt;" in replies.preview("<script> blah").text


async def test_sentences_go_to_claude_and_still_need_confirming(chat, settings, monkeypatch):
    from parsing import llm
    from parsing.tests.fakes import FakeClaude, item

    fake = FakeClaude([item("tracker", tracker="coffee", value=2), item("sleep", bedtime="23:30", wake_time="07:10")],
                      unparsed="ugh")
    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kwargs: fake)

    reply = await chat.sent("two coffees already, slept half eleven to ten past seven, ugh")
    assert reply.text.startswith("📝 <b>Save this?</b>\n• Coffee +2\n• Slept 23:30 → 07:10 (7h 40m)")
    assert "Not logged: “ugh”" in reply.text
    assert await count(Entry) == 0

    calls = await chat.press(buttons(reply)[0], on=reply)
    assert edits(calls)[0].text.startswith("✅ <b>Saved</b>\n• Coffee +2")
    assert len(fake.requests) == 1  # confirming does not ask Claude again
    assert await count(Entry) == 1 and await count(SleepSession) == 1

    calls = await chat.press(buttons(reply)[0], on=reply)  # pressing Save twice
    assert "expired" in edits(calls)[0].text
    assert await count(Entry) == 1


async def test_review_and_ask(chat):
    await chat.say("/coffee 2")
    text = (await chat.sent("/review")).text
    assert text.startswith("🧠 <b>Weekly review</b> · ") and "<b>Next week:</b>" in text and "• " in text
    assert "needs a Claude API key" in (await chat.sent("/ask how much coffee?")).text
    calls = await chat.say("/ask")
    assert sends(calls)[0].text.startswith("💬 <b>Ask about your data</b>")
    calls = await chat.say("how much coffee?", reply_to=chat.session.sent[-1])
    assert "needs a Claude API key" in sends(calls)[0].text  # the answer stays in the chat


def test_sunday_review_is_sent_once(db, monkeypatch):
    from bot import jobs
    from insights import review
    from insights.models import WeeklyReview

    defaults.install()
    monday = review.facts_module.week_start(days.today())
    monkeypatch.setattr(review, "due", lambda now=None: None)
    assert jobs.weekly_review() is None and not WeeklyReview.objects.exists()

    monkeypatch.setattr(review, "due", lambda now=None: monday)
    assert jobs.weekly_review().text.startswith("🧠 <b>Weekly review")
    assert WeeklyReview.objects.get().sent_at is not None


def test_evening_reminder(db):
    from bot import jobs
    from conftest import local

    defaults.install()
    s = AppSettings.load()
    s.timezone = "America/Toronto"
    s.save()
    assert jobs.evening_reminder(local(2026, 11, 14, 20, 59)) is None  # before 21:00

    reply = jobs.evening_reminder(local(2026, 11, 14, 21, 0))
    assert reply.text.startswith("🌙 <b>Evening check-in</b>") and "Coffee" in reply.text and "Sleep" in reply.text
    assert reply.buttons == [[("📝 Fill in now", "v:check")]]
    assert jobs.evening_reminder(local(2026, 11, 14, 23, 0)) is None  # once per day
    # After midnight it is still the same logical day; the next one starts at 04:00.
    assert jobs.evening_reminder(local(2026, 11, 15, 1, 0)) is None
    assert jobs.evening_reminder(local(2026, 11, 15, 12, 0)) is None
    assert jobs.evening_reminder(local(2026, 11, 15, 21, 30)) is not None


def test_reminder_lists_only_what_is_missing_and_can_be_off(db):
    from bot import jobs
    from conftest import local

    defaults.install()
    now = local(2026, 11, 14, 21, 5)
    logbook.apply([TrackerLog(tracker="coffee", value=0), TrackerLog(tracker="mood", value=4)], source="web", now=now)
    text = jobs.evening_reminder(now).text
    assert "Coffee" not in text and "Mood" not in text and "Alcohol" in text  # a logged zero counts as logged

    s = AppSettings.load()
    s.reminder_enabled, s.last_reminder_day = False, None
    s.save()
    assert jobs.evening_reminder(local(2026, 11, 15, 22, 0)) is None


def test_a_reminder_time_after_midnight_belongs_to_the_evening(db):
    from datetime import time

    from bot import jobs
    from conftest import local

    defaults.install()
    s = AppSettings.load()
    s.timezone, s.reminder_time = "America/Toronto", time(0, 30)
    s.save()
    assert not jobs.reminder_due(local(2026, 11, 14, 9, 0))  # morning: 00:30 tonight hasn't come yet
    assert not jobs.reminder_due(local(2026, 11, 14, 23, 59))
    assert jobs.reminder_due(local(2026, 11, 15, 0, 30))
