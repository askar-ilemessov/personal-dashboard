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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Min, Sum

from . import days
from .models import (
    AppSettings,
    Budget,
    Entry,
    Expense,
    ExpenseCategory,
    SleepSession,
    Tracker,
    Workout,
)

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


def _clock_series(start: date, end: date, field: str, anchor_hour: int) -> Series:
    """Time of day as minutes after `anchor_hour`, by wake day. Bedtimes are
    measured from noon so that 23:30 and 00:30 are an hour apart, not 23."""
    tz = AppSettings.load().tz
    out = _empty(start, end)
    sessions = SleepSession.objects.filter(day__range=(start, end), **{f"{field}__isnull": False})
    for s in sessions.order_by("day", "id"):
        local = getattr(s, field).astimezone(tz)
        if out[s.day] is None:  # the first (main) sleep of the day
            out[s.day] = Decimal(((local.hour - anchor_hour) % 24) * 60 + local.minute)
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
    if metric == "expenses.discretionary":
        return _grouped(Expense.objects.filter(recurring__isnull=True), Sum("amount_base"), start, end)
    if metric == "sleep.bedtime":
        return _clock_series(start, end, "bedtime", 12)
    if metric == "sleep.wake":
        return _clock_series(start, end, "wake_time", 0)
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


# --- Metric catalog --------------------------------------------------------


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    kind: str  # habit, counter, duration, scale, sleep, expenses, exercise
    fmt: str  # how a single day's value is shown (see tracking.fmt.value)
    unit: str = ""
    agg: str = "mean"  # how a range is summarised: mean of logged days, or sum

    @property
    def agg_fmt(self) -> str:
        return "percent" if self.kind == "habit" else self.fmt

    @property
    def agg_label(self) -> str:
        if self.kind == "habit":
            return "Completion"
        return "Total" if self.agg == "sum" else "Daily average"

    @property
    def adds_up(self) -> bool:
        """Whether a week/month goal compares against a sum rather than a mean."""
        return self.kind in ("counter", "expenses", "exercise")


_FIXED = {
    "sleep.duration": Metric("sleep.duration", "Sleep", "sleep", "minutes"),
    "expenses.total": Metric("expenses.total", "Spending", "expenses", "money", agg="sum"),
    "exercise.minutes": Metric("exercise.minutes", "Exercise", "exercise", "minutes", agg="sum"),
    "exercise.sessions": Metric("exercise.sessions", "Workouts", "exercise", "count", agg="sum"),
    # Not shown as cards; used by insights.
    "expenses.discretionary": Metric("expenses.discretionary", "Spending without recurring bills", "expenses",
                                     "money", agg="sum"),
    "sleep.bedtime": Metric("sleep.bedtime", "Bedtime", "sleep", "bedtime"),
    "sleep.wake": Metric("sleep.wake", "Wake-up time", "sleep", "clock"),
}
_TRACKER_FMT = {"habit": "habit", "counter": "count", "duration": "minutes", "scale": "count"}


def for_tracker(tracker: Tracker) -> Metric:
    return Metric(f"tracker:{tracker.slug}", tracker.name, tracker.kind, _TRACKER_FMT[tracker.kind], tracker.unit)


def get(key: str) -> Metric:
    if key in _FIXED:
        return _FIXED[key]
    if key.startswith("tracker:"):
        return for_tracker(Tracker.objects.get(slug=key.split(":", 1)[1]))
    raise KeyError(f"Unknown metric {key!r}")


def catalog(habits: bool = True) -> list[Metric]:
    """Everything worth a card or a chart, in display order. Spending is
    left out for someone who switched off every category."""
    trackers = [for_tracker(t) for t in Tracker.objects.filter(is_active=True)]
    out = [_FIXED["sleep.duration"]]
    out += [m for m in trackers if m.kind != "habit"]
    out.append(_FIXED["exercise.minutes"])
    if ExpenseCategory.objects.filter(is_active=True).exists():
        out.append(_FIXED["expenses.total"])
    if habits:
        out += [m for m in trackers if m.kind == "habit"]
    return out


def aggregate(metric: Metric, values: Series) -> Decimal | None:
    """Summarise a range. Unlogged days are left out, never counted as 0."""
    logged = [v for v in values.values() if v is not None]
    if not logged:
        return None
    total = sum(logged, Decimal(0))
    return total if metric.agg == "sum" else total / len(logged)


# --- Goals and budgets -----------------------------------------------------


def period_start(period: str, today: date) -> date:
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    return today


def goal_progress(goal, today: date) -> dict | None:
    try:
        metric = get(goal.metric)
    except (KeyError, Tracker.DoesNotExist):
        return None
    logged = [v for v in series(goal.metric, period_start(goal.period, today), today).values() if v is not None]
    if not logged:
        current = None
    elif goal.period == "day" or metric.adds_up:
        current = sum(logged, Decimal(0))
    else:
        current = sum(logged, Decimal(0)) / len(logged)
    ratio = None if current is None else float(min(Decimal(1), current / goal.target)) if goal.target else 1.0
    return {"goal": goal, "metric": metric, "value": current, "met": goal.is_met(current), "ratio": ratio}


def budgets(today: date) -> list[dict]:
    """This month's spend against each category's current budget."""
    spent = expenses_by_category(today.replace(day=1), today)
    out, seen = [], set()
    rows = Budget.objects.filter(effective_from__lte=today).select_related("category")
    for b in rows.order_by("category__sort_order", "-effective_from"):
        if b.category_id in seen or not b.category.is_active:
            continue
        seen.add(b.category_id)
        if not b.monthly_amount:
            continue  # a zero row means "no budget from this month on"
        used = spent.get(b.category.slug, Decimal(0))
        out.append({
            "category": b.category, "spent": used, "budget": b.monthly_amount,
            "ratio": float(min(Decimal(1), used / b.monthly_amount)) if b.monthly_amount else 1.0,
            "over": used > b.monthly_amount,
        })
    return out


def first_day() -> date | None:
    """Earliest day with anything logged. Periods before it have no data,
    which is different from having zeros."""
    firsts = [m.objects.aggregate(d=Min("day"))["d"] for m in (Entry, Expense, SleepSession, Workout)]
    return min((d for d in firsts if d), default=None)


def comparable(previous: Series, first: date | None) -> Series | None:
    """The previous period, or None when it starts before any data exists
    (comparing against a partly empty period would mislead)."""
    if first is None or not previous or min(previous) < first:
        return None
    return previous
