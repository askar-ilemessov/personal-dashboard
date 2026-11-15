"""Facts: what the stats engine finds in the logged data.

Everything here is deterministic. The functions return plain data with the
numbers already formatted, so that whatever words them (a template or a
language model) never has to calculate or format anything.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from tracking import fmt, metrics
from tracking.models import DayTag, Entry, Expense, Goal, Tracker, Workout

from . import stats

HISTORY_DAYS = 90
RECENT_DAYS = 14

# (cause, effect, lag in days). Lag 1 pairs a day with the following day;
# sleep belongs to the day you wake up, so "tonight's sleep" is lag 1.
PAIRS = [
    ("tracker:screen", "sleep.duration", 1),
    ("tracker:screen", "sleep.bedtime", 1),
    ("tracker:alcohol", "sleep.wake", 1),
    ("tracker:alcohol", "tracker:mood", 1),
    ("exercise.minutes", "tracker:mood", 0),
    ("sleep.duration", "tracker:coffee", 0),
    ("sleep.duration", "tracker:mood", 0),
    ("sleep.duration", "tracker:energy", 0),
]

_MORE = {"sleep.duration": "more sleep", "exercise.minutes": "more exercise", "sleep.bedtime": "a later bedtime",
         "sleep.wake": "a later wake-up"}
_EFFECT = {  # (when it goes up, when it goes down)
    "sleep.duration": ("you tend to sleep longer", "you tend to sleep less"),
    "sleep.bedtime": ("you tend to go to bed later", "you tend to go to bed earlier"),
    "sleep.wake": ("you tend to wake up later", "you tend to wake up earlier"),
}


def _day(d: date) -> str:
    return f"{d:%b} {d.day}"


def _span(start: date, end: date) -> str:
    return _day(start) if start == end else f"{_day(start)}–{_day(end)}"


class _Series:
    """Fetch each metric's series once per report."""

    def __init__(self, start: date, end: date):
        self.start, self.end, self.cache = start, end, {}

    def metric(self, key: str):
        try:
            return metrics.get(key)
        except (KeyError, Tracker.DoesNotExist):
            return None

    def __getitem__(self, key: str) -> dict:
        if key not in self.cache:
            self.cache[key] = metrics.series(key, self.start, self.end)
        return self.cache[key]


# --- correlations --------------------------------------------------------------


@dataclass
class Relationship:
    x: metrics.Metric
    y: metrics.Metric
    lag: int
    result: stats.Correlation | None
    pairs: list
    sentence: str
    detail: str

    @property
    def reliable(self) -> bool:
        return bool(self.result and self.result.reliable)


def _more(m: metrics.Metric) -> str:
    return _MORE.get(m.key, f"more {m.name.lower()}")


def _relationship(x, y, lag, xs, ys) -> Relationship:
    pairs = stats.paired(xs, ys, lag)
    result = stats.correlate(xs, ys, lag)
    when = "the next day" if lag else "the same day"
    if result is None:
        need = stats.MIN_PAIRS
        sentence = f"Not enough data yet to compare {x.name.lower()} with {y.name.lower()}."
        detail = (f"{len(pairs)} days have both logged; {need} are needed."
                  if len(pairs) < need else "One of them never varies, so there is nothing to compare.")
    elif result.strength == "none":
        sentence = f"No visible link between {x.name.lower()} and {y.name.lower()} {when}."
        detail = f"ρ = {result.rho:+.2f} over {result.n} days."
    else:
        up, down = _EFFECT.get(y.key, (f"{y.name.lower()} tends to be higher", f"{y.name.lower()} tends to be lower"))
        effect = up if result.rho > 0 else down
        sentence = f"After days with {_more(x)}, {effect}{' the next day' if lag else ''}."
        if not lag:
            sentence = f"On days with {_more(x)}, {effect}."
        chance = "unlikely to be chance" if result.p < 0.05 else "could still be chance"
        detail = f"{result.strength.capitalize()} link (ρ = {result.rho:+.2f} over {result.n} days), {chance}."
    return Relationship(x, y, lag, result, pairs, sentence, detail)


def correlations(today: date, days: int = HISTORY_DAYS) -> list[Relationship]:
    """The standing questions (screen time vs sleep, alcohol vs wake time...)."""
    data = _Series(today - timedelta(days=days - 1), today)
    out = []
    for xk, yk, lag in PAIRS:
        x, y = data.metric(xk), data.metric(yk)
        if x and y:
            out.append(_relationship(x, y, lag, data[xk], data[yk]))
    return out


def other_relationships(today: date, days: int = HISTORY_DAYS, limit: int = 5) -> list[Relationship]:
    """Same-day links between every other pair of metrics, strongest first.
    Only reliable ones are returned; with this many pairs a few will still
    be coincidence, which the page says."""
    data = _Series(today - timedelta(days=days - 1), today)
    asked = {(a, b) for a, b, _ in PAIRS} | {(b, a) for a, b, _ in PAIRS}
    keys = [m.key for m in metrics.catalog(habits=False)]
    found = []
    for i, xk in enumerate(keys):
        for yk in keys[i + 1:]:
            if (xk, yk) in asked:
                continue
            rel = _relationship(data.metric(xk), data.metric(yk), 0, data[xk], data[yk])
            if rel.reliable:
                found.append(rel)
    return sorted(found, key=lambda r: -abs(r.result.rho))[:limit]


# --- anomalies -----------------------------------------------------------------


def anomalies(today: date, recent: int = RECENT_DAYS, history: int = HISTORY_DAYS) -> list[dict]:
    """Things from the last `recent` days that stand out against the last
    `history` days. Each flag: kind, title, detail, day, metric."""
    data = _Series(today - timedelta(days=history - 1), today)
    since = today - timedelta(days=recent - 1)
    flags = []

    def flag(kind, title, detail, day, metric):
        flags.append({"kind": kind, "title": title, "detail": detail, "day": day, "metric": metric})

    # Spending spikes, ignoring recurring bills (rent is not a surprise).
    spend = data["expenses.discretionary"]
    typical = sorted(stats.clean(spend.values()))
    median_spend = typical[len(typical) // 2] if typical else 0
    for day, value, _ in stats.outliers(spend, since):
        top = Expense.objects.filter(day=day, recurring__isnull=True).order_by("-amount_base").first()
        largest = f" Largest: {top.note or top.category.name} ({fmt.money(top.amount_base)})." if top else ""
        flag("spending", f"Spending spike on {_day(day)}",
             f"{fmt.money(value)} that day; a typical day is {fmt.money(median_spend)}.{largest}", day, "expenses.total")

    # Several short nights in a row.
    goal = Goal.objects.filter(is_active=True, metric="sleep.duration", period="day", comparator="gte").first()
    limit = float(min(goal.target, Decimal(420)) if goal else Decimal(420)) - 60
    for start, end, length in stats.runs(data["sleep.duration"], lambda v: v < limit, 3):
        if end >= since:
            nights = [v for d, v in data["sleep.duration"].items() if start <= d <= end]
            flag("sleep", f"{length} short nights in a row",
                 f"{_span(start, end)}: under {fmt.minutes(limit)} each night, {fmt.minutes(stats.mean(nights))} on average.",
                 end, "sleep.duration")

    # Unusually high counters and durations; runs of low ratings.
    for tracker in Tracker.objects.filter(is_active=True):
        key = f"tracker:{tracker.slug}"
        m = metrics.for_tracker(tracker)
        if tracker.kind in (Tracker.Kind.COUNTER, Tracker.Kind.DURATION):
            logged = sorted(stats.clean(data[key].values()))
            for day, value, _ in stats.outliers(data[key], since)[:2]:
                flag("high", f"Unusually high {tracker.name.lower()} on {_day(day)}",
                     f"{fmt.value(m.fmt, value)}; your median is {fmt.value(m.fmt, logged[len(logged) // 2])}.", day, key)
        elif tracker.kind == Tracker.Kind.SCALE and tracker.min_value is not None:
            low = float(tracker.min_value) + 1
            for start, end, length in stats.runs(data[key], lambda v, low=low: v <= low, 3):
                if end >= since:
                    flag("low", f"Low {tracker.name.lower()} {length} days running",
                         f"{_span(start, end)}: {fmt.number(low)} or below every day.", end, key)

    for b in metrics.budgets(today):
        if b["over"]:
            flag("budget", f"Over budget: {b['category'].name}",
                 f"{fmt.money(b['spent'])} spent of {fmt.money(b['budget'])} this month.", today, "expenses.total")

    # Days with nothing logged at all (today doesn't count; it isn't over).
    week = [today - timedelta(days=i) for i in range(1, 8)]
    first = metrics.first_day()
    logged_days = set(Entry.objects.filter(day__in=week).values_list("day", flat=True))
    empty = [d for d in week if d not in logged_days and first and d >= first]
    if len(empty) >= 2:
        flag("gap", f"Nothing logged on {len(empty)} of the last 7 days",
             "Missing: " + ", ".join(_day(d) for d in sorted(empty)) + ".", max(empty), None)

    return sorted(flags, key=lambda f: f["day"], reverse=True)


# --- weekly facts ----------------------------------------------------------------


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def weekly(start: date, today: date) -> dict:
    """Everything worth saying about the week starting on Monday `start`.
    JSON-serialisable, numbers pre-formatted."""
    end = min(start + timedelta(days=6), today)
    days = (end - start).days + 1
    before_start, before_end = start - timedelta(days=7), start - timedelta(days=1)
    first = metrics.first_day()
    has_before = bool(first and first <= before_start)

    rows = []
    for m in metrics.catalog(habits=False):
        current = metrics.series(m.key, start, end)
        value = metrics.aggregate(m, current)
        logged = {d: v for d, v in current.items() if v is not None}
        row = {"key": m.key, "name": m.name, "kind": m.agg_label.lower(), "value": fmt.value(m.agg_fmt, value),
               "logged_days": len(logged), "days": days, "previous": None, "change": None, "direction": None}
        if value is not None and has_before:
            previous = metrics.aggregate(m, metrics.series(m.key, before_start, before_end))
            delta = stats.change(value, previous)
            if delta is not None:
                row |= {"previous": fmt.value(m.agg_fmt, previous), "change": f"{abs(delta) * 100:.0f}%",
                        "direction": "up" if delta > 0.005 else "down" if delta < -0.005 else "flat",
                        "_delta": delta}
        if m.kind in ("expenses", "exercise"):
            # Event logs: every day counts as logged, so report days with any activity instead.
            row["active_days"] = sum(1 for v in logged.values() if v > 0)
        if logged and m.kind not in ("expenses", "exercise"):
            hi, lo = max(logged, key=logged.get), min(logged, key=logged.get)
            row |= {"highest": f"{fmt.value(m.fmt, logged[hi])} on {hi:%A}", "lowest": f"{fmt.value(m.fmt, logged[lo])} on {lo:%A}"}
        rows.append(row)

    habits = []
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT):
        values = [v for v in metrics.tracker_series(t, start, end).values() if v is not None]
        habits.append({"name": t.name, "done": sum(1 for v in values if v >= 1), "logged": len(values), "days": days})

    goals = []
    for g in Goal.objects.filter(is_active=True):
        try:
            m = metrics.get(g.metric)
        except (KeyError, Tracker.DoesNotExist):
            continue
        if g.period == "day":
            logged = [v for v in metrics.series(g.metric, start, end).values() if v is not None]
            goals.append({"name": g.name, "met_days": sum(1 for v in logged if g.is_met(v)), "logged_days": len(logged)})
        elif g.period == "week":
            progress = metrics.goal_progress(g, end)
            if progress and progress["value"] is not None:
                goals.append({"name": g.name, "result": fmt.value(m.fmt, progress["value"]),
                              "target": fmt.value(m.fmt, g.target), "met": bool(progress["met"])})

    by_category = metrics.expenses_by_category(start, end)
    names = dict(Expense.objects.filter(day__range=(start, end)).values_list("category__slug", "category__name"))
    top = Expense.objects.filter(day__range=(start, end), recurring__isnull=True).order_by("-amount_base").first()
    workouts = list(Workout.objects.filter(day__range=(start, end)).values_list("type", flat=True))

    flags = [f for f in anomalies(end, recent=days) if start <= f["day"] <= end and f["kind"] != "gap"]
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "label": _span(start, end), "days": days,
                   "complete": days == 7},
        "metrics": rows,
        "habits": habits,
        "goals": goals,
        "spending": {
            "by_category": [{"name": names.get(slug, slug), "amount": fmt.money(amount)}
                            for slug, amount in sorted(by_category.items(), key=lambda kv: -kv[1])[:5]],
            "largest": {"what": top.note or top.category.name, "amount": fmt.money(top.amount_base),
                        "day": f"{top.day:%A}"} if top else None,
        },
        "workouts": {"count": len(workouts), "types": sorted(set(workouts))},
        "tags": sorted(set(DayTag.objects.filter(day__range=(start, end)).values_list("tag__slug", flat=True))),
        "flags": [{"title": f["title"], "detail": f["detail"], "kind": f["kind"]} for f in flags],
        "relationships": [f"{r.sentence} {r.detail}" for r in correlations(end) if r.reliable],
    }
