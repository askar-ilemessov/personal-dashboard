"""The bot's menu: one message that is edited in place as you tap, like a
small app. Each screen is a plain function returning (text, buttons); taps
that log something re-render the screen with a status line on top and an
Undo button. Nothing here sends anything: bot/handlers.py delivers it.

Callback data (Telegram allows 64 bytes):
    v:<screen>            show a screen; "gym.legs" is screen "gym" with argument "legs"
    l:<screen>:<text>     log <text> with the shared grammar, then show <screen>
    u:<screen>:<op id>    undo an operation, then show <screen>
    h:<slug>              toggle a habit, then show the habits screen
    p:<question>          ask for typed input (see QUESTIONS), e.g. p:spend.groceries
"""

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from html import escape

from django.db.models import Sum

from parsing.describe import describe
from parsing.grammar import parse, parse_duration
from tracking import days, fmt, logbook, metrics
from tracking.logbook import LogError, TrackerLog
from tracking.models import (
    AppSettings,
    Entry,
    Expense,
    ExpenseCategory,
    Goal,
    Operation,
    SleepSession,
    Tracker,
    Workout,
)

SOURCE = "telegram"

ICONS = {
    "coffee": "☕", "alcohol": "🍷", "junk": "🍟", "screen": "📱", "mood": "🙂", "energy": "⚡",
    "read": "📖", "meditate": "🧘", "vitamins": "💊", "stretch": "🤸", "water": "💧", "steps": "👟",
}
KIND_ICONS = {"counter": "🔢", "duration": "⏱", "scale": "🎚", "habit": "🔁"}
WORKOUT_ICONS = {"push": "💪", "pull": "🧗", "legs": "🦵", "cardio": "🏃", "other": "🤸"}
WORKOUT_MINUTES = (30, 45, 60, 75, 90)


def icon(tracker: Tracker) -> str:
    return ICONS.get(tracker.slug, KIND_ICONS.get(tracker.kind, "•"))


@dataclass
class Reply:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    toast: str = ""  # shown briefly over the chat when a button was tapped
    screen: str = ""  # set for menu screens: which one this is


@dataclass
class Flash:
    """The status line above a screen after a tap."""

    text: str  # plain text; escaped when rendered
    mark: str = "✓"
    op: int | None = None


def _data(*parts) -> str | None:
    data = ":".join(str(p) for p in parts)
    return data if len(data.encode()) <= 64 else None


def _button(label: str, *parts) -> tuple[str, str] | None:
    data = _data(*parts)
    return (label, data) if data else None


def _rows(buttons, per_row: int) -> list[list[tuple[str, str]]]:
    buttons = [b for b in buttons if b]
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def _value(key: str, form: str, day) -> str:
    v = metrics.value(key, day)
    return "—" if v is None else f"<b>{fmt.value(form, v)}</b>"


def _today():
    return days.today(AppSettings.load())


def _trackers(kind: str | None = None):
    qs = Tracker.objects.filter(is_active=True)
    return list(qs.filter(kind=kind) if kind else qs)


def _habit_count(day) -> tuple[int, int]:
    habits = _trackers("habit")
    done = Entry.objects.filter(day=day, tracker__in=habits, value__gte=1).values("tracker").distinct().count()
    return done, len(habits)


def _spending_on() -> bool:
    return ExpenseCategory.objects.filter(is_active=True).exists()


def summary(intent, settings) -> str:
    """One plain-text line for something just logged, with today's running
    total for counters: "Coffee +1 · 3 today"."""
    text = describe(intent, settings)
    if isinstance(intent, TrackerLog):
        tracker = logbook.resolve_tracker(intent.tracker)
        if tracker.kind == Tracker.Kind.COUNTER:
            day = intent.day or days.today(settings)
            total = metrics.value(f"tracker:{tracker.slug}", day)
            text += f" · {fmt.number(total or 0)} {'today' if not intent.day else 'that day'}"
    return text


# --- Screens ----------------------------------------------------------------


def home(arg=""):
    day = _today()
    lines = [f"<b>{day:%A}, {day:%B} {day.day}</b>", ""]
    lines.append(f"😴 Sleep {_value('sleep.duration', 'minutes', day)}")
    for kind in ("counter", "duration", "scale"):
        parts = [f"{icon(t)} {escape(t.name)} {_value(f'tracker:{t.slug}', metrics.for_tracker(t).fmt, day)}"
                 for t in _trackers(kind)]
        if parts:
            lines.append(" · ".join(parts))
    activity = [f"🏋️ Exercise {_value('exercise.minutes', 'minutes', day)}"]
    if _spending_on():
        activity.append(f"💸 Spent {_value('expenses.total', 'money', day)}")
    lines.append(" · ".join(activity))
    done, total = _habit_count(day)
    if total:
        lines.append(f"✅ Habits <b>{done} of {total}</b>")

    goals = [g for g in (metrics.goal_progress(g, day) for g in Goal.objects.filter(is_active=True)) if g]
    if goals:
        lines += ["", "<b>Goals</b>"]
        for g in goals:
            m, goal, value = g["metric"], g["goal"], g["value"]
            pending = goal.comparator == Goal.Comparator.AT_LEAST and goal.period != "day"
            mark = "▫️" if value is None else "✅" if g["met"] else "⏳" if pending else "⚠️"
            shown = "not logged" if value is None else f"{fmt.value(m.fmt, value)} of {fmt.value(m.fmt, goal.target)}"
            lines.append(f"{mark} {escape(goal.name)} · {shown}")

    missing = len(metrics.unlogged(day))
    rows = _rows([_button(f"{icon(t)} {t.name} +1", "l", "home", t.slug) for t in _trackers("counter")], 3)
    scales = _trackers("scale")
    if scales:
        label = " & ".join(t.name for t in scales[:2]) + ("…" if len(scales) > 2 else "")
        rows.append([(f"{icon(scales[0])} {label}", "v:rate")])
    rows.append([(f"✅ Habits {done}/{total}" if total else "✅ Habits", "v:habits"), ("😴 Sleep", "v:sleep")])
    rows.append([("💸 Spend", "v:spend"), ("🏋️ Workout", "v:gym")] if _spending_on() else [("🏋️ Workout", "v:gym")])
    rows.append([(f"📝 Fill in {missing} left" if missing else "📝 All logged", "v:check"), ("➕ More", "v:more")])
    rows.append([("📈 Week", "v:week"), ("💬 Ask", "p:ask"), ("❔ Help", "v:help")])
    return "\n".join(lines), rows


def rate(arg=""):
    day = _today()
    lines, rows = ["<b>How are you feeling?</b>", ""], []
    for t in _trackers("scale"):
        current = metrics.value(f"tracker:{t.slug}", day)
        lines.append(f"{icon(t)} {escape(t.name)}: {'—' if current is None else f'<b>{fmt.number(current)}</b>'}")
        lo, hi = int(t.min_value or 1), int(t.max_value or 5)
        labels = [f"• {n} •" if current == n else str(n) for n in range(lo, hi + 1)]
        labels[0] = f"{icon(t)} {labels[0]}"
        rows += _rows([_button(label, "l", "rate", f"{t.slug} {n}") for label, n in zip(labels, range(lo, hi + 1))], 6)
    if not rows:
        lines.append("No scale trackers. Add one in the app under Settings → Trackers.")
    return "\n".join(lines), rows


def habits(arg=""):
    day = _today()
    logged = dict(Entry.objects.filter(day=day, tracker__kind="habit").values_list("tracker__slug", "value"))
    lines, rows = [], []
    for t in _trackers("habit"):
        value = logged.get(t.slug)
        mark = "⬜" if value is None else "✅" if value >= 1 else "❌"
        current, _ = metrics.habit_streaks(t, day)
        streak = f" · 🔥 {current}" if current > 1 else ""
        lines.append(f"{mark} {escape(t.name)}{streak}")
        rows.append([b for b in [_button(f"{mark} {t.name}{streak}", "h", t.slug)] if b])
    if not lines:
        return "<b>Habits</b>\n\nNo habits yet. Add one in the app under Settings → Trackers.", []
    done, total = _habit_count(day)
    return f"<b>Habits today · {done} of {total}</b>\n<i>Tap to mark done; tap again for not done.</i>", rows


def sleep(arg=""):
    s = AppSettings.load()
    day = days.today(s)
    lines = ["<b>Sleep</b>", ""]
    open_night = SleepSession.objects.filter(wake_time__isnull=True, bedtime__isnull=False).order_by("-bedtime").first()
    stretches = metrics.night(day)
    total = metrics.value("sleep.duration", day)
    if stretches:
        times = fmt.sleep_times(stretches, s.tz)
        lines.append(f"Last night: {times} · <b>{fmt.minutes(total)}</b>" if total is not None else f"Last night: {times}")
        if len(stretches) > 1:
            lines.append(f"<i>Woke {'once' if len(stretches) == 2 else f'{len(stretches) - 1} times'} in the night</i>")
    else:
        lines.append("Last night: not logged")
    if open_night:
        lines.append(f"🌙 In bed since {fmt.clock(open_night.bedtime, s.tz)}")
    week = [v for v in metrics.series("sleep.duration", day - timedelta(days=6), day).values() if v is not None]
    if week:
        lines.append(f"7-day average: <b>{fmt.minutes(sum(week) / len(week))}</b> over {len(week)} nights")
    rows = [[("🌙 Going to bed", "l:sleep:slept"), ("☀️ Woke up", "l:sleep:woke")],
            [("✏️ Enter times", "p:sleep")]]
    return "\n".join(lines), rows


def spend(arg=""):
    day = _today()
    month = Expense.objects.filter(day__gte=day.replace(day=1), day__lte=day).aggregate(t=Sum("amount_base"))["t"]
    lines = ["<b>Spending</b>", "",
             f"Today {_value('expenses.total', 'money', day)} · This month <b>{fmt.money(month or 0)}</b>"]
    budgets = metrics.budgets(day)
    if budgets:
        lines.append("")
        for b in budgets:
            filled = round(b["ratio"] * 8)
            bar = "▰" * filled + "▱" * (8 - filled)
            mark = " ⚠️" if b["over"] else ""
            lines.append(f"{escape(b['category'].name)}  {bar}  {fmt.money(b['spent'])} of {fmt.money(b['budget'])}{mark}")
    recent = Expense.objects.filter(day=day).select_related("category")[:3]
    if recent:
        lines += ["", "<b>Today</b>"]
        lines += [f"• {fmt.money(e.amount, e.currency)} {escape(e.category.name)}"
                  f"{' · ' + escape(e.note) if e.note else ''}" for e in recent]
    lines += ["", "<i>Pick a category, then type the amount.</i>"]
    categories = ExpenseCategory.objects.filter(is_active=True)
    return "\n".join(lines), _rows([_button(c.name, "p", f"spend.{c.slug}") for c in categories], 3)


def gym(arg=""):
    day = _today()
    if arg:
        label = dict(Workout.Type.choices).get(arg, arg.title())
        rows = _rows([_button(f"{n} min", "l", "gym", f"{arg} {n}") for n in WORKOUT_MINUTES], 3)
        rows.append([("✏️ Other length", f"p:gym.{arg}")])
        return f"<b>{WORKOUT_ICONS.get(arg, '🏋️')} {escape(label)}</b>\n\nHow long?", rows
    start = day - timedelta(days=day.weekday())
    week = Workout.objects.filter(day__gte=start, day__lte=day)
    minutes = sum(w.duration_min or 0 for w in week)
    lines = ["<b>Workouts</b>", "", f"This week: <b>{week.count()}</b> · {fmt.minutes(minutes)}"]
    last = Workout.objects.first()
    if last:
        when = "today" if last.day == day else "yesterday" if last.day == day - timedelta(days=1) else f"{last.day:%b} {last.day.day}"
        lines.append(f"Last: {last.get_type_display()}{' · ' + fmt.minutes(last.duration_min) if last.duration_min else ''} · {when}")
    buttons = [_button(f"{WORKOUT_ICONS.get(v, '')} {label}".strip(), "v", f"gym.{v}") for v, label in Workout.Type.choices]
    return "\n".join(lines), _rows(buttons, 3)


def more(arg=""):
    rows = [[(f"{icon(t)} {t.name}…", f"p:track.{t.slug}")] for t in _trackers("duration") if _data("p", f"track.{t.slug}")]
    rows.append([("📝 Note for today", "p:note"), ("🏷 Tag the day", "p:tag")])
    rows.append([("🧠 Weekly review", "v:review")])
    text = ("<b>More</b>\n\nDurations and notes need a few words from you: tap one and reply.\n"
            "You can also just type anything, e.g. <code>2 coffees, spent 12 groceries lunch</code>.")
    return text, rows


def check(arg=""):
    """Everything not logged today, each with one-tap answers."""
    day = _today()
    missing = set(metrics.unlogged(day))
    rows, names = [], []
    for t in _trackers():
        if f"tracker:{t.slug}" not in missing:
            continue
        names.append(f"{icon(t)} {escape(t.name)}")
        if t.kind == "scale":
            lo, hi = int(t.min_value or 1), int(t.max_value or 5)
            rows += _rows([_button(f"{icon(t)} {n}" if n == lo else str(n), "l", "check", f"{t.slug} {n}")
                           for n in range(lo, hi + 1)], 5)
        elif t.kind == "counter":
            rows.append([b for b in [_button(f"{icon(t)} None", "l", "check", f"{t.slug} 0"),
                                     *(_button(str(n), "l", "check", f"{t.slug} {n}") for n in (1, 2, 3))] if b])
        elif t.kind == "habit":
            rows.append([b for b in [_button(f"{icon(t)} {t.name} ✓", "l", "check", t.slug),
                                     _button("✗", "l", "check", f"{t.slug} no")] if b])
        else:
            rows.append([b for b in [_button(f"{icon(t)} {t.name}…", "p", f"track.{t.slug}")] if b])
    if "sleep.duration" in missing:
        names.append("😴 Sleep")
        rows.append([("😴 Sleep…", "v:sleep")])
    if not names:
        return "<b>Check-in</b>\n\nEverything is logged for today. 🎉", []
    text = (f"<b>Check-in · {len(names)} left</b>\n{', '.join(names)}\n\n"
            "<i>Each row answers the item named on its first button.</i>")
    return text, rows


def week(arg=""):
    end = _today()
    start, before = end - timedelta(days=6), end - timedelta(days=13)
    first = metrics.first_day()
    lines = [f"<b>Last 7 days</b> · {start:%b} {start.day} – {end:%b} {end.day}", ""]
    for m in metrics.catalog():
        series = metrics.series(m.key, before, end)
        current = {d: v for d, v in series.items() if d >= start}
        previous = metrics.comparable({d: v for d, v in series.items() if d < start}, first)
        now = metrics.aggregate(m, current)
        if now is None:
            lines.append(f"{escape(m.name)} —")
            continue
        logged = sum(1 for v in current.values() if v is not None)
        if m.kind == "habit":
            done = sum(1 for v in current.values() if v is not None and v >= 1)
            lines.append(f"{escape(m.name)} <b>{done} of {logged}</b> days")
            continue
        text = f"{escape(m.name)} <b>{fmt.value(m.agg_fmt, now)}</b> {'total' if m.agg == 'sum' else 'avg'}"
        before_value = metrics.aggregate(m, previous) if previous else None
        if before_value:
            change = float((now - before_value) / before_value)
            if abs(change) >= 0.005:
                text += f" {'▲' if change > 0 else '▼'}{abs(change) * 100:.0f}%"
        lines.append(text)
    lines += ["", "<i>▲▼ compare with the 7 days before.</i>"]
    return "\n".join(lines), [[("🧠 Weekly review", "v:review")]]


def help_(arg=""):
    return HELP, []


def review(arg=""):
    text = ("<b>Weekly review</b>\n\nA short write-up of this week so far, from your numbers. "
            "It arrives as its own message so you can keep it.")
    return text, [[("✍️ Write it now", "review")]]


HELP = """<b>How this works</b>

This message is your dashboard. Tap to log; it updates in place.

<b>Or just type</b>, and I'll show what I understood before saving:
<code>2 coffees, spent 12.5 groceries lunch</code>
<code>yesterday slept 23:30-7:10</code>
<code>legs 60</code> · <code>mood 4</code> · <code>screen 3h20</code>

<b>Commands</b>
/menu — this dashboard at the bottom of the chat
/habits /sleep /spend /gym /week — jump to a screen
/review — this week's review
/ask how did I sleep this month?
/undo — undo the last thing logged here
Any tracker works as a command too: <code>/coffee 2</code>"""


SCREENS = {"home": home, "rate": rate, "habits": habits, "sleep": sleep, "spend": spend, "gym": gym,
           "more": more, "check": check, "week": week, "help": help_, "review": review}
PARENT = {"gym": "gym", "review": "more"}  # where Back goes from a screen with an argument / a sub-screen


def render(screen: str = "home", flash: Flash | None = None) -> Reply:
    name, _, arg = screen.partition(".")
    if name not in SCREENS:
        name, arg, screen = "home", "", "home"
    text, rows = SCREENS[name](arg)
    buttons = []
    if flash:
        text = f"{flash.mark} {escape(flash.text)}\n\n{text}"
        if flash.op:
            buttons.append([b for b in [_button("↩ Undo", "u", screen, flash.op)] if b])
    buttons += rows
    if name != "home":
        back = PARENT.get(name, "home") if arg or name == "review" else "home"
        nav = [("‹ Back", f"v:{back}")] if back != "home" else []
        buttons.append([*nav, ("🏠 Home", "v:home")])
    return Reply(text, [row for row in buttons if row], toast=f"{flash.mark} {flash.text}"[:190] if flash else "",
                 screen=screen)


# --- Actions ------------------------------------------------------------------


def log(screen: str, text: str) -> Reply:
    """Save `text` (grammar only: buttons never need Claude) and redraw."""
    parsed = parse(text)
    if not parsed.ok:
        return render(screen, Flash("; ".join(parsed.errors) or "Nothing to log.", mark="⚠️"))
    return apply(screen, parsed.intents, text)


def apply(screen: str, intents, raw_text: str) -> Reply:
    return _apply(screen, intents, raw_text)[0]


def _apply(screen: str, intents, raw_text: str) -> tuple[Reply, bool]:
    try:
        op = logbook.apply(intents, source=SOURCE, raw_text=raw_text)
    except LogError as e:
        return render(screen, Flash(str(e), mark="⚠️")), False
    settings = AppSettings.load()
    return render(screen, Flash(" · ".join(summary(i, settings) for i in intents), op=op.pk)), True


def undo(screen: str, op_id: int) -> Reply:
    op = Operation.objects.filter(pk=op_id).first()
    if op is None or op.undone_at:
        return render(screen, Flash("That was undone already.", mark="↩"))
    try:
        logbook.undo(op)
    except LogError as e:
        return render(screen, Flash(str(e), mark="⚠️"))
    return render(screen, Flash(f"Undone: {op.raw_text}" if op.raw_text else "Undone", mark="↩"))


def toggle_habit(slug: str) -> Reply:
    """Unlogged or not done -> done; done -> not done."""
    try:
        tracker = logbook.resolve_tracker(slug)
    except LogError as e:
        return render("habits", Flash(str(e), mark="⚠️"))
    current = metrics.value(f"tracker:{tracker.slug}", days.today())
    done = not (current is not None and current >= 1)
    op = logbook.apply([TrackerLog(tracker=tracker.slug, value=1 if done else 0)], source=SOURCE,
                       raw_text=f"{tracker.slug}{'' if done else ' no'}")
    return render("habits", Flash(f"{tracker.name}: {'done' if done else 'not done'}", op=op.pk))


def undo_last(screen: str = "home") -> Reply:
    op = Operation.objects.filter(source=SOURCE, undone_at__isnull=True).first()
    if op is None:
        return render(screen, Flash("Nothing to undo.", mark="↩"))
    return undo(screen, op.pk)


# --- Questions (typed input) --------------------------------------------------


@dataclass(frozen=True)
class Question:
    text: str  # HTML
    placeholder: str
    screen: str  # shown after the answer is saved


def question(key: str) -> Question | None:
    kind, _, arg = key.partition(".")
    if kind == "spend":
        category = ExpenseCategory.objects.filter(slug=arg, is_active=True).first()
        if category:
            return Question(f"💸 <b>{escape(category.name)}</b> · how much?", "12.50 lunch", "spend")
    elif kind == "track":
        tracker = Tracker.objects.filter(slug=arg, is_active=True).first()
        if tracker:
            hint = "3h20" if tracker.kind == "duration" else "2"
            return Question(f"{icon(tracker)} <b>{escape(tracker.name)}</b> · how much today?", hint, "home")
    elif kind == "gym":
        label = dict(Workout.Type.choices).get(arg)
        if label:
            return Question(f"{WORKOUT_ICONS.get(arg, '🏋️')} <b>{label}</b> · how many minutes?", "50", "gym")
    elif kind == "sleep":
        return Question("😴 <b>When did you sleep?</b>\n<i>Bed and wake times; start with “yesterday” "
                        "for an earlier night. Woke up in the night? 22:00-1:00, 1:30-6:40</i>", "23:30-7:10", "sleep")
    elif kind == "note":
        return Question("📝 <b>Note for today</b>", "Felt great after the run", "home")
    elif kind == "tag":
        return Question("🏷 <b>Tag today</b>\n<i>One or more words, e.g. sick travel.</i>", "travel", "home")
    elif kind == "ask":
        return Question("💬 <b>Ask about your data</b>", "How did I sleep this month?", "home")
    return None


def grammar_for(key: str, answer: str) -> str:
    """The grammar text an answer stands for: "12.50 lunch" to spend.groceries
    becomes "spent 12.50 groceries lunch"."""
    kind, _, arg = key.partition(".")
    answer = " ".join(answer.split())
    if kind == "spend":
        amount, _, rest = answer.partition(" ")
        return f"spent {amount} {arg} {rest}".strip()
    if kind == "track":
        return f"{arg} {answer}"
    if kind == "gym":
        return f"{arg} {answer}"
    if kind == "sleep":
        first, _, rest = answer.partition(" ")
        if first.lower() in ("slept", "woke", "bed"):
            return answer
        return f"yesterday slept {rest}" if first.lower() == "yesterday" else f"slept {answer}"
    if kind == "note":
        return f"note {answer}"
    if kind == "tag":
        return "tag " + " ".join(f"#{w.lstrip('#')}" for w in answer.split())
    return answer


def _check(key: str, answer: str) -> str:
    """Answers that need a number must start with one; otherwise the grammar
    would happily log "legs" with no length, or read "a while" as a note."""
    kind, _, arg = key.partition(".")
    first = (answer.split() or [""])[0]
    if kind == "spend":
        try:
            Decimal(first.replace(",", "."))
        except ArithmeticError:
            return "Start with the amount, like 12.50 lunch."
    elif kind == "gym" and parse_duration(first) is None:
        return "Send the length in minutes, like 50."
    elif kind == "track":
        tracker = Tracker.objects.filter(slug=arg).first()
        if tracker and tracker.kind == "duration" and parse_duration(answer) is None:
            return "Send a length, like 3h20 or 45m."
    return ""


def answer(key: str, text: str) -> tuple[Reply, bool]:
    """Log the answer to a question. Returns the screen to show and whether
    it was saved (if not, the question stays open)."""
    q = question(key)
    screen = q.screen if q else "home"
    problem = _check(key, text)
    if problem:
        return render(screen, Flash(problem, mark="⚠️")), False
    parsed = parse(grammar_for(key, text))
    if not parsed.ok:
        return render(screen, Flash("; ".join(parsed.errors) or "I couldn't read that.", mark="⚠️")), False
    return _apply(screen, parsed.intents, text)
