"""Messages that stay in the chat: what you typed and saved (a receipt with
Undo), the weekly review, answers to questions, reminders, the welcome.
Everything you tap through is the menu in bot/screens.py, which is edited
in place instead. Plain synchronous functions returning a Reply, so they can
be tested without Telegram; bot/handlers.py delivers them.

Callback data used here:
    ok:<draft id>, no:<draft id>   save or drop a previewed message
    r:<op id>                      undo a receipt
    review                         write and send the weekly review
"""

from html import escape

from insights import ask as ask_module
from insights import review as review_module
from parsing import parser
from parsing.models import Draft
from tracking import logbook, metrics
from tracking.logbook import LogError
from tracking.models import AppSettings, Operation, TelegramLink, Tracker

from . import screens
from .screens import SOURCE, Reply

HELP = screens.HELP

NOT_LINKED = ("👋 This bot logs to a Personal dashboard account.\n\nTo connect yours, open "
              "<b>Settings → Telegram</b> in the app and press <b>Connect Telegram</b>.")


def link(code: str, telegram_id: int, name: str) -> tuple[Reply, TelegramLink | None]:
    """Redeem a code from Settings. Runs with no acting user: the code says whose account it is."""
    linked = TelegramLink.redeem(code, telegram_id, name)
    if linked is None:
        return Reply("That link has expired or was already used. Open <b>Settings → Telegram</b> "
                     "in the app for a new one."), None
    return Reply(f"✅ Connected to <b>{escape(linked.user.username)}</b>.\n\nTap the buttons below to log, "
                 "or just type what happened. To disconnect, use Settings in the app."), linked


def command(word: str, args: str) -> Reply:
    """A typed /command for a tracker. Returns the menu to show: the result
    of logging, or the screen that asks for the missing part."""
    args = args.strip()
    if not args:
        try:
            tracker = logbook.resolve_tracker(word)
        except LogError:
            tracker = None
        if tracker and tracker.kind == Tracker.Kind.SCALE:
            return screens.render("rate")
        if tracker and tracker.kind == Tracker.Kind.DURATION:
            return screens.render("more", screens.Flash(f"How long? Tap {tracker.name} below, or send "
                                                        f"/{word} 3h20", mark="⏱"))
    return screens.log("home", f"{word} {args}".strip())


# --- Typed messages: preview, then a receipt ---------------------------------------


def _lines(items) -> str:
    return "\n".join(f"• {escape(line)}" for line in items)


def preview(text: str) -> Reply:
    """Free text: show what would be saved and ask first. Uses the shared
    parser, so sentences the grammar can't read go to Claude."""
    result = parser.interpret(text)
    if not result.ok:
        problems = "\n".join(escape(e) for e in (result.errors or ["Nothing to log."]))
        return Reply(f"🤔 {problems}\n\n<i>Try something like</i> <code>2 coffees, spent 12 groceries lunch</code>",
                     [[("❔ What can I type?", "v:help")]])
    draft = parser.save_draft(result, SOURCE)
    ignored = f"\n\n<i>Not logged: “{escape(result.ignored)}”</i>" if result.ignored else ""
    return Reply(f"📝 <b>Save this?</b>\n{_lines(result.lines)}{ignored}",
                 [[("✓ Save", f"ok:{draft.pk}"), ("✕ Cancel", f"no:{draft.pk}")]])


def confirm(draft_id: int) -> Reply:
    draft = Draft.objects.filter(pk=draft_id).first()
    if draft is None:
        return Reply("That preview has expired. Send the message again.")
    try:
        intents = parser.draft_intents(draft)
        op = logbook.apply(intents, source=SOURCE, raw_text=draft.text)
    except LogError as e:
        return Reply(f"⚠️ {escape(str(e))}")
    draft.delete()
    settings = AppSettings.load()
    return Reply(f"✅ <b>Saved</b>\n{_lines(screens.summary(i, settings) for i in intents)}",
                 [[("↩ Undo", f"r:{op.pk}")]])


def cancel(draft_id: int) -> None:
    Draft.objects.filter(pk=draft_id).delete()


def undo_receipt(op_id: int, text: str) -> Reply:
    """Undo from a receipt: the receipt stays, struck through."""
    op = Operation.objects.filter(pk=op_id).first()
    if op is not None and not op.undone_at:
        try:
            logbook.undo(op)
        except LogError as e:
            return Reply(f"⚠️ {escape(str(e))}")
    items = [line.removeprefix("• ") for line in text.splitlines()[1:]]
    return Reply("↩ <b>Undone</b>\n" + "\n".join(f"<s>{escape(line)}</s>" for line in items))


# --- Kept in the chat: review, answers, reminders --------------------------------------------


def review(week_start=None) -> Reply:
    """The weekly review (written now if it doesn't exist yet)."""
    r = review_module.generate(week_start)
    label = r.facts["period"]["label"]
    lines = [f"🧠 <b>Weekly review</b> · {escape(label)}", "", escape(r.summary), ""]
    lines += [f"• {escape(o)}" for o in r.observations]
    lines += ["", f"<b>Next week:</b> {escape(r.suggestion)}"]
    return Reply("\n".join(lines))


def ask(question: str) -> Reply:
    if not question.strip():
        return Reply("Ask something like <code>/ask how did I sleep this month?</code>")
    answer = ask_module.ask(question)
    if answer.error:
        return Reply("⚠️ " + escape(answer.error))
    text = f"💬 <i>{escape(question.strip())}</i>\n\n{escape(answer.text)}"
    if answer.unverified:
        text += "\n\n<i>Check these figures, no query returned them: " + escape(", ".join(answer.unverified)) + "</i>"
    return Reply(text)


def reminder(day) -> Reply | None:
    """Evening nudge listing what is still unlogged. Its button turns the
    message into the check-in screen."""
    names = []
    for key in metrics.unlogged(day):
        try:
            names.append(metrics.get(key).name)
        except (KeyError, Tracker.DoesNotExist):
            continue
    if not names:
        return None
    return Reply("🌙 <b>Evening check-in</b>\nStill to log today: " + escape(", ".join(names)),
                 [[("📝 Fill in now", "v:check")]])
