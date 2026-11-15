"""What the bot says. Plain synchronous functions that return a Reply, so
they can be tested without Telegram; bot/handlers.py only delivers them.

Callback data used by the buttons:
    q:<text>      log <text> through the shared grammar, e.g. q:coffee
    undo:<id>     undo an operation
    habit:<slug>  toggle a habit for today
    ok:<id>, no:<id>  confirm or cancel a previewed free-text message (Draft id)
    habits, today, menu
"""

from dataclasses import dataclass, field
from datetime import timedelta
from html import escape

from insights import ask as ask_module
from insights import review as review_module
from parsing import parser
from parsing.describe import describe
from parsing.grammar import parse
from parsing.models import Draft
from tracking import days, fmt, logbook, metrics
from tracking.logbook import LogError, TrackerLog
from tracking.models import AppSettings, Entry, Goal, Operation, Tracker

SOURCE = "telegram"

HELP = """<b>Quick commands</b>
/coffee · /drink · /junk — add one (or <code>/coffee 2</code>)
/spent 12.5 food lunch
/gym legs 60
/slept · /woke — now, or <code>/woke 7:10</code>
/screen 3h20
/mood 4 · /energy 3
/habits — check off habits
/today · /week — summaries
/review — this week's review
/ask how did I sleep this month? — ask about your data
/undo — undo the last thing logged here
/menu — one-tap buttons

Any tracker works as a command, and you can also just type:
<code>2 coffees, spent 12.5 food milk, legs 60</code>
<code>yesterday slept 23:30-7:10</code>"""


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, callback data)


def _undo_button(op: Operation):
    return [[("↩ Undo", f"undo:{op.pk}")]]


def _line(intent, settings) -> str:
    text = describe(intent, settings)
    if isinstance(intent, TrackerLog):
        tracker = logbook.resolve_tracker(intent.tracker)
        if tracker.kind == Tracker.Kind.COUNTER:
            _, day = (intent.day, intent.day) if intent.day else (None, days.today(settings))
            total = metrics.value(f"tracker:{tracker.slug}", day)
            when = "today" if not intent.day else "that day"
            text += f" · {fmt.number(total or 0)} {when}"
    return escape(text)


def _problems(errors) -> Reply:
    return Reply("⚠️ " + "\n".join(escape(e) for e in errors))


def _saved(intents, raw_text: str) -> Reply:
    try:
        op = logbook.apply(intents, source=SOURCE, raw_text=raw_text)
    except LogError as e:
        return _problems([str(e)])
    settings = AppSettings.load()
    return Reply("\n".join(f"✓ {_line(i, settings)}" for i in intents), _undo_button(op))


def log(text: str) -> Reply:
    """Parse with the grammar and save straight away (commands and buttons)."""
    parsed = parse(text)
    if not parsed.ok:
        return _problems(parsed.errors or ["Nothing to log."])
    return _saved(parsed.intents, text)


def command(word: str, args: str) -> Reply:
    """/word args. A scale tracker with no value answers with buttons."""
    args = args.strip()
    if not args:
        try:
            tracker = logbook.resolve_tracker(word)
        except LogError:
            tracker = None
        if tracker and tracker.kind == Tracker.Kind.SCALE:
            return scale(tracker)
        if tracker and tracker.kind == Tracker.Kind.DURATION:
            return Reply(f"How long? For example <code>/{escape(word)} 3h20</code>")
    return log(f"{word} {args}".strip())


def scale(tracker: Tracker) -> Reply:
    lo, hi = int(tracker.min_value or 1), int(tracker.max_value or 5)
    row = [(str(n), f"q:{tracker.slug} {n}") for n in range(lo, hi + 1)]
    return Reply(f"{escape(tracker.name)}?", [row])


def preview(text: str) -> Reply:
    """Free text: show what would be saved and ask first. Uses the shared
    parser, so sentences the grammar can't read go to Claude."""
    result = parser.interpret(text)
    if not result.ok:
        reply = _problems(result.errors or ["Nothing to log."])
        if not result.intents:
            reply.text += "\n\n/help shows what I understand."
        return reply
    draft = parser.save_draft(result, SOURCE)
    lines = "\n".join(f"+ {escape(line)}" for line in result.lines)
    ignored = f"\n\nNot logged: “{escape(result.ignored)}”" if result.ignored else ""
    return Reply(f"Save this?\n{lines}{ignored}", [[("✓ Save", f"ok:{draft.pk}"), ("✕ Cancel", f"no:{draft.pk}")]])


def confirm(draft_id: int) -> Reply:
    draft = Draft.objects.filter(pk=draft_id).first()
    if draft is None:
        return Reply("That preview has expired. Send the message again.")
    try:
        intents = parser.draft_intents(draft)
    except LogError as e:
        return _problems([str(e)])
    reply = _saved(intents, draft.text)
    draft.delete()
    return reply


def cancel(draft_id: int) -> Reply:
    Draft.objects.filter(pk=draft_id).delete()
    return Reply("Cancelled. Nothing was saved.")


def undo(op_id: int) -> Reply:
    op = Operation.objects.filter(pk=op_id).first()
    if op is None:
        return Reply("That entry is gone already.")
    try:
        logbook.undo(op)
    except LogError as e:
        return Reply(escape(str(e)))
    what = f": {escape(op.raw_text)}" if op.raw_text else ""
    return Reply(f"↩ Undone{what}")


def undo_last() -> Reply:
    op = Operation.objects.filter(source=SOURCE, undone_at__isnull=True).first()
    if op is None:
        return Reply("Nothing to undo.")
    return undo(op.pk)


def menu() -> Reply:
    counters = [
        (f"{t.name} +1", f"q:{t.slug}")
        for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.COUNTER)
    ]
    rows = [counters[i : i + 3] for i in range(0, len(counters), 3)]
    rows.append([("😴 Going to bed", "q:slept"), ("⏰ Woke up", "q:woke")])
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.SCALE):
        lo, hi = int(t.min_value or 1), int(t.max_value or 5)
        rows.append([(f"{t.name} {n}", f"q:{t.slug} {n}") for n in range(lo, hi + 1)])
    rows.append([("Habits", "habits"), ("Today", "today")])
    return Reply("What happened?", rows)


def habits() -> Reply:
    today = days.today()
    logged = dict(Entry.objects.filter(day=today, tracker__kind="habit").values_list("tracker__slug", "value"))
    rows, lines = [], []
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT):
        value = logged.get(t.slug)
        mark = "▫️" if value is None else "✅" if value >= 1 else "✖️"
        current, _ = metrics.habit_streaks(t, today)
        streak = f" · {current}-day streak" if current > 1 else ""
        lines.append(f"{mark} {escape(t.name)}{streak}")
        rows.append([(f"{mark} {t.name}", f"habit:{t.slug}")])
    if not rows:
        return Reply("No habits yet. Add one in Settings → Trackers.")
    return Reply("<b>Habits today</b> (tap to toggle)\n" + "\n".join(lines), rows)


def toggle_habit(slug: str) -> Reply:
    """Unlogged or not done -> done; done -> not done."""
    today = days.today()
    current = metrics.value(f"tracker:{slug}", today)
    try:
        logbook.apply([TrackerLog(tracker=slug, value=0 if current and current >= 1 else 1)], source=SOURCE)
    except (LogError, Tracker.DoesNotExist) as e:
        return Reply(escape(str(e)))
    return habits()


def _goal_line(progress) -> str:
    m, goal, value = progress["metric"], progress["goal"], progress["value"]
    if value is None:
        return f"▫️ {escape(goal.name)}: not logged"
    mark = "✅" if progress["met"] else "❗"
    return f"{mark} {escape(goal.name)}: {fmt.value(m.fmt, value)} / {fmt.value(m.fmt, goal.target)}"


def today() -> Reply:
    s = AppSettings.load()
    day = days.today(s)
    lines = [f"<b>{day:%A, %B} {day.day}</b>"]
    missing = []
    for m in metrics.catalog(habits=False):
        value = metrics.value(m.key, day)
        if value is None:
            missing.append(m.name)
        else:
            lines.append(f"{escape(m.name)}: <b>{fmt.value(m.fmt, value)}</b>")
    logged = dict(Entry.objects.filter(day=day, tracker__kind="habit").values_list("tracker__name", "value"))
    habit_names = list(Tracker.objects.filter(is_active=True, kind="habit").values_list("name", flat=True))
    if habit_names:
        done = [n for n in habit_names if logged.get(n) is not None and logged[n] >= 1]
        missing += [n for n in habit_names if n not in logged]
        lines.append(f"Habits: <b>{len(done)} of {len(habit_names)}</b> done")
    goals = [g for g in (metrics.goal_progress(g, day) for g in Goal.objects.filter(is_active=True)) if g]
    if goals:
        lines += ["", "<b>Goals</b>", *(_goal_line(g) for g in goals)]
    if missing:
        lines += ["", "<b>Not logged yet</b>", escape(", ".join(missing))]
    else:
        lines += ["", "Everything is logged. 🎉"]
    return Reply("\n".join(lines), [[("Log something", "menu"), ("Habits", "habits")]])


def week() -> Reply:
    s = AppSettings.load()
    end = days.today(s)
    start, before = end - timedelta(days=6), end - timedelta(days=13)
    first = metrics.first_day()
    lines = [f"<b>Last 7 days</b> ({start:%b} {start.day} – {end:%b} {end.day})"]
    for m in metrics.catalog():
        series = metrics.series(m.key, before, end)
        current = {d: v for d, v in series.items() if d >= start}
        previous = metrics.comparable({d: v for d, v in series.items() if d < start}, first)
        now = metrics.aggregate(m, current)
        if now is None:
            lines.append(f"{escape(m.name)}: not logged")
            continue
        logged = sum(1 for v in current.values() if v is not None)
        label = "total" if m.agg == "sum" else "done" if m.kind == "habit" else "avg"
        text = f"{escape(m.name)}: <b>{fmt.value(m.agg_fmt, now)}</b> {label}"
        if m.kind == "habit":
            done = sum(1 for v in current.values() if v is not None and v >= 1)
            text = f"{escape(m.name)}: <b>{done} of {logged}</b> logged days"
        elif m.agg != "sum" and logged < 7:
            text += f" over {logged} day{'s' if logged != 1 else ''}"
        before_value = metrics.aggregate(m, previous) if previous else None
        if before_value and m.kind != "habit":
            change = float((now - before_value) / before_value)
            if abs(change) >= 0.005:
                text += f" ({'▲' if change > 0 else '▼'}{abs(change) * 100:.0f}% vs prior week)"
        lines.append(text)
    return Reply("\n".join(lines))


def review(week_start=None) -> Reply:
    """The weekly review (written now if it doesn't exist yet)."""
    r = review_module.generate(week_start)
    label = r.facts["period"]["label"]
    lines = [f"<b>Weekly review · {escape(label)}</b>", "", escape(r.summary), ""]
    lines += [f"• {escape(o)}" for o in r.observations]
    lines += ["", f"<b>Try next week:</b> {escape(r.suggestion)}"]
    return Reply("\n".join(lines))


def ask(question: str) -> Reply:
    if not question.strip():
        return Reply("Ask something like <code>/ask how did I sleep this month?</code>")
    answer = ask_module.ask(question)
    if answer.error:
        return Reply("⚠️ " + escape(answer.error))
    text = escape(answer.text)
    if answer.unverified:
        text += "\n\n<i>Check these figures, no query returned them: " + escape(", ".join(answer.unverified)) + "</i>"
    return Reply(text)
