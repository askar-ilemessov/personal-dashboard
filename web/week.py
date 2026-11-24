"""The Week page (Monday to Sunday as seven day cards) and the Day view
(one day: what's missing, what was logged, add forms).

Reads through tracking.metrics; every control logs through views.log."""

from datetime import date, timedelta

from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET

from tracking import days, fmt, metrics
from tracking.models import (
    AppSettings,
    DayNote,
    DayTag,
    Entry,
    Expense,
    ExpenseCategory,
    Goal,
    SleepSession,
    Tracker,
    Workout,
)

from .home import log_rows, week_strip
from .views import _parse_day

# Colour per kind of metric (Momentum: colour means an area).
AREA = {"sleep": "sleep", "exercise": "sleep", "habit": "habit", "counter": "money", "duration": "money",
        "expenses": "money", "scale": "mood"}


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_label(monday: date, today: date) -> str:
    sunday = monday + timedelta(days=6)
    span = f"{monday:%B} {monday.day} – {sunday.day}" if monday.month == sunday.month \
        else f"{monday:%b} {monday.day} – {sunday:%b} {sunday.day}"
    this = monday_of(today)
    when = "this week" if monday == this else "last week" if monday == this - timedelta(days=7) else ""
    return f"{span} · {when}" if when else span


# --- One day, summarised -------------------------------------------------------------


def day_card(d: date, today: date, s: AppSettings, trackers: list[Tracker]) -> dict:
    """What a day card shows: logged things with an area dot, and what's missing."""
    items, missing = [], []
    sleep = metrics.value("sleep.duration", d)
    if sleep is None:
        missing.append("Sleep")
    else:
        items.append({"area": "sleep", "label": "Sleep", "value": fmt.minutes(sleep)})
    for t in trackers:
        v = metrics.value(f"tracker:{t.slug}", d)
        if v is None:
            missing.append(t.name)
        elif t.kind == Tracker.Kind.HABIT:
            items.append({"area": "habit", "label": t.name, "value": "✓" if v >= 1 else "–", "done": v >= 1})
        else:
            items.append({"area": AREA[t.kind], "label": t.name, "value": fmt.value(metrics.for_tracker(t).fmt, v)})
    for w in Workout.objects.filter(day=d):
        items.append({"area": "sleep", "label": w.get_type_display(),
                      "value": fmt.minutes(w.duration_min) if w.duration_min else "✓"})
    if Expense.objects.filter(day=d).exists():
        items.append({"area": "money", "label": "Spent", "value": fmt.money(metrics.value("expenses.total", d))})
    elif metrics.spending_unlogged(d):
        missing.append("Spending")
    elif ExpenseCategory.objects.filter(is_active=True).exists():
        items.append({"area": "money", "label": "Spent", "value": fmt.money(0)})
    return {"day": d, "today": d == today, "future": d > today, "items": items,
            "missing": [] if d > today else missing,
            "url": reverse("web:day", args=[d.isoformat()])}


# --- Week ----------------------------------------------------------------------------------


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def week_tiles(monday: date, today: date) -> list[dict]:
    end = min(monday + timedelta(days=6), today)
    if end < monday:
        return []
    tiles = []

    sleep = metrics.series("sleep.duration", monday, end)
    logged = [v for v in sleep.values() if v is not None]
    goal = Goal.objects.filter(is_active=True, metric="sleep.duration").first()
    note = f"{len(logged)} nights logged"
    if goal and logged:
        met = sum(1 for v in logged if goal.is_met(v))
        note = f"{fmt.minutes(goal.target)}+ on {met} of {len(logged)} nights"
    tiles.append({"area": "sleep", "label": "Sleep", "value": fmt.minutes(_mean(logged)) if logged else "—", "note": note})

    best = None
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT):
        series = metrics.series(f"tracker:{t.slug}", monday, end)
        done = sum(1 for v in series.values() if (v or 0) >= 1)
        if best is None or done > best[0]:
            best = (done, t)
    if best:
        tiles.append({"area": "habit", "label": best[1].name, "value": f"{best[0]} of 7",
                      "note": "Your most-done habit"})

    minutes = sum(v or 0 for v in metrics.series("exercise.minutes", monday, end).values())
    sessions = int(sum(v or 0 for v in metrics.series("exercise.sessions", monday, end).values()))
    goal = Goal.objects.filter(is_active=True, metric="exercise.sessions", period="week").first()
    tiles.append({"area": "night", "label": "Exercise", "value": fmt.minutes(minutes),
                  "note": f"{sessions} workout{'s' if sessions != 1 else ''}"
                          + (f", goal {fmt.number(goal.target)}" if goal else "")})

    if ExpenseCategory.objects.filter(is_active=True).exists():
        total = sum(v or 0 for v in metrics.series("expenses.total", monday, end).values())
        days_logged = Expense.objects.filter(day__range=(monday, end)).values("day").distinct().count()
        span = (end - monday).days + 1
        tiles.append({"area": "money", "label": "Spent", "value": fmt.money(total),
                      "note": f"on {days_logged} of {span} days"})
    return tiles


def week(request, start=None):
    s = AppSettings.load()
    today = days.today(s)
    monday = monday_of(_parse_day(start) if start else today)
    if monday > today:
        return redirect("web:week")
    trackers = list(Tracker.objects.filter(is_active=True))
    this = monday_of(today)
    return render(request, "web/week.html", {
        "nav": "week", "monday": monday, "number": monday.isocalendar().week, "label": week_label(monday, today),
        "is_this_week": monday == this,
        "prev": monday - timedelta(days=7), "next": monday + timedelta(days=7) if monday < this else None,
        "tiles": week_tiles(monday, today),
        "cards": [day_card(monday + timedelta(days=i), today, s, trackers) for i in range(7)],
    })


# --- Day -------------------------------------------------------------------------------------


def _timeline(d: date, s: AppSettings) -> list[dict]:
    """Everything logged on `d`, in the order it happened. `key` and `pk` let
    a row be deleted through views.row_delete (which can be undone)."""
    tz = s.tz
    out = []
    for e in Entry.objects.filter(day=d).select_related("tracker"):
        m = metrics.for_tracker(e.tracker)
        if e.tracker.kind == Tracker.Kind.HABIT:
            text = f"{e.tracker.name}: {'done' if e.value >= 1 else 'not done'}"
        else:
            text = f"{e.tracker.name} {fmt.value(m.fmt, e.value)}"
        out.append({"at": e.occurred_at, "text": text, "sub": e.note, "area": AREA[e.tracker.kind],
                    "key": "dayentry", "pk": e.pk, "source": e.get_source_display()})
    for n in SleepSession.objects.filter(day=d):
        at = n.wake_time or n.bedtime
        bed = fmt.clock(n.bedtime, tz) if n.bedtime else "?"
        wake = fmt.clock(n.wake_time, tz) if n.wake_time else "still asleep"
        text = f"Slept {fmt.minutes(n.duration_min)}" if n.duration_min is not None else "Went to bed"
        out.append({"at": at, "text": text, "sub": f"{bed} → {wake}", "area": "sleep", "key": "sleep", "pk": n.pk,
                    "source": n.get_source_display()})
    for w in Workout.objects.filter(day=d):
        text = f"{w.get_type_display()}" + (f" · {fmt.minutes(w.duration_min)}" if w.duration_min else "")
        out.append({"at": w.occurred_at, "text": text, "sub": w.notes, "area": "sleep", "key": "workout", "pk": w.pk,
                    "source": w.get_source_display()})
    for x in Expense.objects.filter(day=d).select_related("category"):
        out.append({"at": x.occurred_at, "text": f"{fmt.money(x.amount, x.currency)} · {x.category.name}",
                    "sub": x.note, "area": "money", "key": "expense", "pk": x.pk, "source": x.get_source_display()})
    out.sort(key=lambda i: i["at"])
    for i in out:
        i["time"] = fmt.clock(i["at"], tz)
    return out


def _glance(d: date) -> list[dict]:
    sleep = metrics.value("sleep.duration", d)
    out = [{"area": "sleep", "label": "Sleep", "value": fmt.minutes(sleep) if sleep is not None else "—"}]
    scale = Tracker.objects.filter(is_active=True, kind=Tracker.Kind.SCALE).first()
    if scale:
        v = metrics.value(f"tracker:{scale.slug}", d)
        out.append({"area": "mood", "label": scale.name, "value": fmt.number(v) if v is not None else "—"})
    ex = metrics.value("exercise.minutes", d)
    out.append({"area": "night", "label": "Exercise", "value": fmt.minutes(ex) if ex else "—"})
    if ExpenseCategory.objects.filter(is_active=True).exists():
        out.append({"area": "money", "label": "Spent", "value": fmt.money(metrics.value("expenses.total", d) or 0)})
    return out


def day(request, day):
    s = AppSettings.load()
    d, today = _parse_day(day), days.today(s)
    if d > today:
        return redirect("web:day", today.isoformat())
    rows = log_rows(d, today, s)
    return render(request, "web/day.html", {
        "nav": "week", "day": d, "today": today, "is_today": d == today, "is_yesterday": d == today - timedelta(days=1),
        "prev": d - timedelta(days=1), "next": d + timedelta(days=1) if d < today else None,
        "week": week_strip(d, today), "week_number": d.isocalendar().week, "monday": monday_of(d),
        "glance": _glance(d), "missing": [r for r in rows if not r["logged"]],
        "logged_count": sum(1 for r in rows if r["logged"]), "row_count": len(rows),
        "timeline": _timeline(d, s), "day_field": "" if d == today else d.isoformat(),
        "categories": ExpenseCategory.objects.filter(is_active=True), "workout_types": Workout.Type.choices,
        "note": DayNote.objects.filter(day=d).first(), "tags": DayTag.objects.filter(day=d).select_related("tag"),
    })


@require_GET
def day_jump(request):
    """The date field on the Day view: go to any past day."""
    try:
        d = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        return redirect("web:today")
    return redirect("web:day", d.isoformat())
