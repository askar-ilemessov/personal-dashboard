"""Home (the Today page): this week's strip, four area tiles, the streak
card, what yesterday is still missing, today's log list and tasks.

Reads through tracking.metrics; logging goes through views.log like every
other control. Tasks are edited here directly (they are not logged rows)."""

from datetime import date, timedelta
from decimal import Decimal

from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from insights import highlights
from tracking import days, fmt, metrics
from tracking import tasks as task_text
from tracking.models import AppSettings, ExpenseCategory, Goal, SleepSession, Task, Tracker

from . import feeds
from .views import _error, _events, _first_steps

WEEKDAY_LETTERS = "MTWTFSS"


def today(request):
    s = AppSettings.load()
    day = days.today(s)
    yesterday = day - timedelta(days=1)
    left = [r for r in log_rows(yesterday, day, s) if not r["logged"]]
    goals = [g for g in (metrics.goal_progress(g, day) for g in Goal.objects.filter(is_active=True)) if g]
    return render(request, "web/today.html", {
        "nav": "today", "day": day, "yesterday": yesterday,
        "week": week_strip(day, day), "week_number": day.isocalendar().week,
        "week_start": day - timedelta(days=day.weekday()), "week_end": day + timedelta(days=6 - day.weekday()),
        "tiles": tiles(day, s), "highlight": highlights.highlight(day),
        "left": left, "today_rows": log_rows(day, day, s),
        "tasks": task_list(day, s), "goals": goals, "budgets": metrics.budgets(day),
        "weather": feeds.weather(s.weather_lat, s.weather_lon, s.timezone) if s.weather_lat is not None else None,
        "events": feeds.events_on(s.calendar_url, day, s.tz) if s.calendar_url else None,
        "calendar_on": bool(s.calendar_url),
        "first_steps": _first_steps(s, metrics.first_day()),
    })


def week_strip(selected: date, today: date) -> list[dict]:
    """Monday to Sunday around `selected`, with how much of each day is logged."""
    monday = selected - timedelta(days=selected.weekday())
    out = []
    for i in range(7):
        d = monday + timedelta(days=i)
        out.append({
            "day": d, "letter": WEEKDAY_LETTERS[i], "today": d == today, "future": d > today,
            "selected": d == selected and d != today,
            "share": 0 if d > today else round(metrics.logged_share(d) * 100),
            "url": reverse("web:day", args=[d.isoformat()]),
        })
    return out


# --- Tiles -----------------------------------------------------------------------


def _goal(metric_key: str, period: str | None = None) -> Goal | None:
    qs = Goal.objects.filter(is_active=True, metric=metric_key)
    return (qs.filter(period=period) if period else qs).first()


def tiles(day: date, s: AppSettings) -> list[dict]:
    out = []

    sleep = metrics.value("sleep.duration", day)
    goal = _goal("sleep.duration")
    if sleep is None:
        note = "Not logged yet"
    elif goal and goal.is_met(sleep):
        note = "Goal met"
    elif goal:
        note = f"{fmt.minutes(abs(goal.target - sleep))} {'under' if sleep < goal.target else 'over'} goal"
    else:
        note = "Last night"
    out.append({"area": "sleep", "label": "Sleep", "value": fmt.minutes(sleep) if sleep is not None else "—",
                "note": note, "url": reverse("web:sleep")})

    monday = day - timedelta(days=day.weekday())
    sessions = int(sum(v or 0 for v in metrics.series("exercise.sessions", monday, day).values()))
    goal = _goal("exercise.sessions", "week")
    minutes_today = metrics.value("exercise.minutes", day)
    out.append({"area": "habit", "label": "Workouts this week",
                "value": f"{sessions} of {fmt.number(goal.target)}" if goal else str(sessions),
                "note": f"{fmt.minutes(minutes_today)} today" if minutes_today else "None today",
                "url": reverse("web:exercise")})

    if ExpenseCategory.objects.filter(is_active=True).exists():
        spent = metrics.value("expenses.total", day) or 0
        budgets = metrics.budgets(day)
        if budgets:
            left = sum(max(b["budget"] - b["spent"], 0) for b in budgets)
            note = f"{fmt.money(left)} left in {day:%B}"
        else:
            note = "Today"
        out.append({"area": "money", "label": "Spent today", "value": fmt.money(spent), "note": note,
                    "url": reverse("web:expenses")})

    scales = list(Tracker.objects.filter(is_active=True, kind=Tracker.Kind.SCALE))
    if scales:
        first, rest = scales[0], scales[1:]
        v = metrics.value(f"tracker:{first.slug}", day)
        hi = fmt.number(first.max_value or 5)
        others = []
        for t in rest[:1]:
            w = metrics.value(f"tracker:{t.slug}", day)
            others.append(f"{t.name} {fmt.number(w) if w is not None else '—'}")
        out.append({"area": "mood", "label": first.name, "value": f"{fmt.number(v)} / {hi}" if v is not None else "—",
                    "note": ", ".join(others) or ("Not logged yet" if v is None else "Today"),
                    "url": reverse("web:tracker", args=[first.slug])})
    return out


# --- Log list ----------------------------------------------------------------------


def log_rows(d: date, today: date, s: AppSettings) -> list[dict]:
    """One row per active tracker plus sleep, for day `d`, with what its
    control needs. `logged` is False for anything not logged that day."""
    day_param = "" if d == today else d.isoformat()
    rows = [_sleep_row(d, today, s, day_param)]
    for t in Tracker.objects.filter(is_active=True):
        m = metrics.for_tracker(t)
        v = metrics.value(m.key, d)
        row = {"kind": t.kind, "name": t.name, "slug": t.slug, "value": v, "logged": v is not None,
               "display": fmt.value(m.fmt, v) if v is not None else "", "day": day_param,
               "url": reverse("web:tracker", args=[t.slug])}
        if t.kind == Tracker.Kind.COUNTER:
            row["minus"] = f"{max(Decimal(v or 0) - 1, Decimal(0)).normalize():f}"
            row["unit"] = t.unit
        elif t.kind == Tracker.Kind.SCALE:
            row["scale"] = list(range(int(t.min_value or 1), int(t.max_value or 5) + 1))
        rows.append(row)
    if ExpenseCategory.objects.filter(is_active=True).exists():
        rows.append(_spend_row(d, day_param))
    return rows


def _spend_row(d: date, day_param: str) -> dict:
    """Spending is an event log; the row is "logged" once the day has an
    expense or a "nothing spent" (so forgotten days get asked about)."""
    unlogged = metrics.spending_unlogged(d)
    total = metrics.value("expenses.total", d) or 0
    return {"kind": "spend", "name": "Spending", "slug": "spend", "day": day_param, "logged": not unlogged,
            "display": fmt.money(total) if total else "Nothing spent", "value": total,
            "url": reverse("web:expenses"), "add_url": reverse("web:expenses") + "#spend-add-title"}


def _sleep_row(d: date, today: date, s: AppSettings, day_param: str) -> dict:
    night = SleepSession.objects.filter(day=d, wake_time__isnull=False).order_by("-wake_time").first()
    row = {"kind": "sleep", "name": "Sleep", "slug": "sleep", "day": day_param, "url": reverse("web:sleep"),
           "logged": night is not None, "value": night.duration_min if night else None, "in_bed": ""}
    if night:
        times = f"{fmt.clock(night.bedtime, s.tz)} → " if night.bedtime else ""
        row["display"] = f"{fmt.minutes(night.duration_min)}" if night.duration_min is not None else "Woke up"
        row["times"] = times + fmt.clock(night.wake_time, s.tz)
    elif d == today:
        open_night = (SleepSession.objects.filter(wake_time__isnull=True, bedtime__isnull=False)
                      .order_by("-bedtime").first())
        if open_night:
            row["in_bed"] = fmt.clock(open_night.bedtime, s.tz)
    return row


# --- Tasks ---------------------------------------------------------------------------


def _due_label(due: date | None, day: date) -> tuple[str, bool]:
    if due is None:
        return "", False
    if due < day:
        return "Overdue", True
    if due == day:
        return "Today", False
    if due == day + timedelta(days=1):
        return "Tomorrow", False
    if due < day + timedelta(days=7):
        return f"{due:%a}", False
    return f"{due:%b} {due.day}", False


def task_list(day: date, s: AppSettings) -> dict:
    open_tasks = []
    for t in Task.objects.filter(done_at__isnull=True):
        label, late = _due_label(t.due, day)
        open_tasks.append({"task": t, "due": label, "late": late})
    done = [t for t in Task.objects.filter(done_at__isnull=False).order_by("-done_at")[:20]
            if days.logical_day(t.done_at, s) == day]
    return {"open": open_tasks, "done": done}


@require_POST
def task_add(request):
    title, due = task_text.parse(request.POST.get("text", ""), days.today())
    if not title:
        return _error("Write what needs doing, e.g. call the bank fri.")
    Task.objects.create(title=title, due=due)
    when = f" · due {_due_label(due, days.today())[0].lower()}" if due else ""
    return _events(refresh={}, toast={"message": f"Added: {title}{when}"})


@require_POST
def task_toggle(request, pk):
    task = get_object_or_404(Task, pk=pk)
    task.done_at = None if task.done_at else timezone.now()
    task.save(update_fields=["done_at"])
    return _events(refresh={}, toast={"message": f"Done: {task.title}" if task.done_at else f"Reopened: {task.title}"})


@require_POST
def task_delete(request, pk):
    task = get_object_or_404(Task, pk=pk)
    task.delete()
    return _events(refresh={}, toast={"message": f"Deleted: {task.title}"})

