"""Read side: daily series by metric key.

Metric keys (also used by Goal.metric):
    tracker:<slug>      the tracker's daily value
    sleep.duration      minutes slept, by wake day
    expenses.total      spend in the default currency
    exercise.minutes    total workout minutes
    exercise.sessions   number of workouts

A value of None means "not logged". Trackers and sleep return None for days
without rows. Expenses and exercise are event logs where no row genuinely
means none, so they return 0.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum

from . import days
from .models import Entry, Expense, SleepSession, Tracker, Workout

Series = dict[date, Decimal | None]


def _empty(start: date, end: date, fill=None) -> Series:
    return {d: fill for d in days.daterange(start, end)}


def tracker_series(tracker: Tracker, start: date, end: date) -> Series:
    out = _empty(start, end)
    rows = Entry.objects.filter(tracker=tracker, day__range=(start, end)).order_by("occurred_at", "id")
    is_sum = tracker.aggregation == Tracker.Aggregation.SUM
    for day, value in rows.values_list("day", "value"):
        out[day] = (out[day] or Decimal(0)) + value if is_sum else value
    return out


def sleep_series(start: date, end: date) -> Series:
    out = _empty(start, end)
    sessions = SleepSession.objects.filter(
        day__range=(start, end), bedtime__isnull=False, wake_time__isnull=False
    )
    for s in sessions:
        out[s.day] = (out[s.day] or Decimal(0)) + Decimal(s.duration_min)
    return out


def _grouped(queryset, aggregate, start, end) -> Series:
    out = _empty(start, end, Decimal(0))
    for row in queryset.filter(day__range=(start, end)).values("day").annotate(v=aggregate):
        out[row["day"]] = Decimal(row["v"] or 0)
    return out


def series(metric: str, start: date, end: date) -> Series:
    if metric.startswith("tracker:"):
        return tracker_series(Tracker.objects.get(slug=metric.split(":", 1)[1]), start, end)
    if metric == "sleep.duration":
        return sleep_series(start, end)
    if metric == "expenses.total":
        return _grouped(Expense.objects, Sum("amount_base"), start, end)
    if metric == "exercise.minutes":
        return _grouped(Workout.objects, Sum("duration_min"), start, end)
    if metric == "exercise.sessions":
        return _grouped(Workout.objects, Count("id"), start, end)
    raise KeyError(f"Unknown metric {metric!r}")


def value(metric: str, day: date) -> Decimal | None:
    return series(metric, day, day)[day]


def unlogged(day: date) -> list[str]:
    """Metric keys with nothing logged for `day`: active trackers and sleep."""
    logged = set(Entry.objects.filter(day=day).values_list("tracker__slug", flat=True))
    missing = [
        f"tracker:{slug}"
        for slug in Tracker.objects.filter(is_active=True).values_list("slug", flat=True)
        if slug not in logged
    ]
    if not SleepSession.objects.filter(day=day, wake_time__isnull=False).exists():
        missing.append("sleep.duration")
    return missing


def habit_streaks(tracker: Tracker, today: date) -> tuple[int, int]:
    """(current, longest) run of consecutive done days. Today not being
    logged yet does not break the current streak. A day marked not done
    (today included) or an unlogged earlier day does."""
    logged = dict(
        Entry.objects.filter(tracker=tracker, day__lte=today)
        .order_by("occurred_at", "id")
        .values_list("day", "value")
    )
    done = {day for day, v in logged.items() if v >= 1}
    longest = run = 0
    previous = None
    for day in sorted(done):
        run = run + 1 if previous and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    cursor = today if today in logged else today - timedelta(days=1)
    current = 0
    while cursor in done:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


def expenses_by_category(start: date, end: date) -> dict[str, Decimal]:
    out = defaultdict(Decimal)
    rows = Expense.objects.filter(day__range=(start, end)).values("category__slug").annotate(v=Sum("amount_base"))
    for row in rows:
        out[row["category__slug"]] = row["v"]
    return dict(out)
