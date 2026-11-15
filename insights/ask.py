"""Ask my data: Claude answers questions by calling a fixed set of
read-only query functions. It never sees the database, writes no SQL, and
is told to quote figures from the results rather than compute its own.
Any number in the answer that no query returned is reported back.
"""

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import anthropic
from django.conf import settings as django_settings

from tracking import days, fmt, metrics
from tracking.models import AppSettings, DayNote, DayTag, Expense, SleepSession, Tracker, Workout

from . import stats
from .review import numbers_in

MAX_ROUNDS = 8
MAX_RANGE_DAYS = 800
MAX_ROWS = 100


class ToolError(Exception):
    """Bad arguments. The message goes back to the model so it can retry."""


# --- argument helpers ----------------------------------------------------------


def _range(args) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(args["start"]), date.fromisoformat(args["end"])
    except (KeyError, TypeError, ValueError):
        raise ToolError("start and end must be dates like 2026-11-14.") from None
    if start > end:
        raise ToolError("start is after end.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ToolError(f"The range is too long; use at most {MAX_RANGE_DAYS} days.")
    return start, end


def _metric(key) -> metrics.Metric:
    try:
        return metrics.get(key)
    except (KeyError, Tracker.DoesNotExist, AttributeError):
        known = ", ".join(m.key for m in _all_metrics())
        raise ToolError(f"Unknown metric {key!r}. Available: {known}.") from None


def _all_metrics() -> list[metrics.Metric]:
    extra = ["exercise.sessions", "expenses.discretionary", "sleep.bedtime", "sleep.wake"]
    return [*metrics.catalog(), *(metrics.get(k) for k in extra)]


def _num(value) -> float | None:
    return None if value is None else round(float(value), 2)


def _summary(m: metrics.Metric, series: dict) -> dict:
    logged = {d: v for d, v in series.items() if v is not None}
    out = {"metric": m.key, "days_in_range": len(series), "days_logged": len(logged)}
    if not logged:
        return out | {"note": "Nothing was logged in this range."}
    total = sum(logged.values(), Decimal(0))
    hi, lo = max(logged, key=logged.get), min(logged, key=logged.get)
    average = total / len(logged)
    out |= {
        "average_per_logged_day": fmt.value(m.agg_fmt if m.kind == "habit" else m.fmt, average),
        "highest": {"value": fmt.value(m.fmt, logged[hi]), "day": hi.isoformat()},
        "lowest": {"value": fmt.value(m.fmt, logged[lo]), "day": lo.isoformat()},
    }
    if m.agg == "sum" or m.kind == "counter":
        out["total"] = fmt.value(m.fmt, total)
    if m.kind == "habit":
        out["days_done"] = sum(1 for v in logged.values() if v >= 1)
    return out


# --- the tools ---------------------------------------------------------------------


def get_series(args):
    m = _metric(args.get("metric"))
    start, end = _range(args)
    series = metrics.series(m.key, start, end)
    return {"metric": m.key, "unit": m.fmt,
            "days": [{"day": d.isoformat(), "weekday": f"{d:%a}", "value": None if v is None else fmt.value(m.fmt, v)}
                     for d, v in series.items()],
            "note": "A null value means that day was not logged (not zero)."}


def summarize(args):
    m = _metric(args.get("metric"))
    start, end = _range(args)
    return _summary(m, metrics.series(m.key, start, end))


def compare_periods(args):
    m = _metric(args.get("metric"))
    a = metrics.series(m.key, *_range({"start": args.get("start_a"), "end": args.get("end_a")}))
    b = metrics.series(m.key, *_range({"start": args.get("start_b"), "end": args.get("end_b")}))
    out = {"metric": m.key, "period_a": _summary(m, a), "period_b": _summary(m, b)}
    delta = stats.change(metrics.aggregate(m, b), metrics.aggregate(m, a))
    if delta is not None:
        word = "higher" if delta > 0 else "lower"
        basis = "total" if m.agg == "sum" else "average"
        out["change"] = f"Period B's {basis} is {abs(delta) * 100:.0f}% {word} than period A's."
    return out


def correlation(args):
    x, y = _metric(args.get("metric_x")), _metric(args.get("metric_y"))
    start, end = _range(args)
    lag = int(args.get("lag_days") or 0)
    if not 0 <= lag <= 7:
        raise ToolError("lag_days must be between 0 and 7.")
    xs, ys = metrics.series(x.key, start, end), metrics.series(y.key, start, end)
    result = stats.correlate(xs, ys, lag)
    pairs = len(stats.paired(xs, ys, lag))
    if result is None:
        return {"result": "not enough data", "days_with_both_logged": pairs, "days_needed": stats.MIN_PAIRS}
    return {
        "spearman_rho": f"{result.rho:+.2f}", "days_with_both_logged": result.n, "strength": result.strength,
        "direction": result.direction, "likely_real": result.p < 0.05,
        "reading": f"Higher {x.name.lower()} goes with {'higher' if result.rho > 0 else 'lower'} {y.name.lower()}"
                   f"{f' {lag} day(s) later' if lag else ''}." if result.strength != "none" else "No visible link.",
        "caution": "A correlation in one person's history, not proof of cause.",
    }


_COMPARE = {"lt": lambda v, t: v < t, "lte": lambda v, t: v <= t, "gt": lambda v, t: v > t,
            "gte": lambda v, t: v >= t, "eq": lambda v, t: v == t}


def find_days(args):
    m = _metric(args.get("metric"))
    start, end = _range(args)
    test = _COMPARE.get(args.get("comparison"))
    if test is None:
        raise ToolError("comparison must be one of lt, lte, gt, gte, eq.")
    try:
        threshold = Decimal(str(args.get("value")))
    except ArithmeticError:
        raise ToolError("value must be a number (minutes for durations and clock times).") from None
    series = metrics.series(m.key, start, end)
    hits = [(d, v) for d, v in series.items() if v is not None and test(v, threshold)]
    return {"metric": m.key, "matching_days": len(hits), "days_logged": sum(1 for v in series.values() if v is not None),
            "days": [{"day": d.isoformat(), "weekday": f"{d:%a}", "value": fmt.value(m.fmt, v)} for d, v in hits[:MAX_ROWS]]}


def weekday_averages(args):
    m = _metric(args.get("metric"))
    start, end = _range(args)
    buckets: dict[int, list] = {i: [] for i in range(7)}
    for d, v in metrics.series(m.key, start, end).items():
        if v is not None:
            buckets[d.weekday()].append(v)
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return {"metric": m.key, "weekdays": [
        {"weekday": names[i], "days_logged": len(vs),
         "average": fmt.value(m.agg_fmt if m.kind == "habit" else m.fmt, sum(vs, Decimal(0)) / len(vs)) if vs else None}
        for i, vs in buckets.items()]}


def expense_breakdown(args):
    start, end = _range(args)
    by_category = metrics.expenses_by_category(start, end)
    names = dict(Expense.objects.values_list("category__slug", "category__name"))
    total = sum(by_category.values(), Decimal(0))
    return {"total": fmt.money(total), "expenses": Expense.objects.filter(day__range=(start, end)).count(),
            "categories": [{"category": names.get(slug, slug), "amount": fmt.money(amount),
                            "share": f"{amount / total * 100:.0f}%" if total else "0%"}
                           for slug, amount in sorted(by_category.items(), key=lambda kv: -kv[1])]}


def list_entries(args):
    start, end = _range(args)
    kind, search = args.get("kind"), (args.get("search") or "").strip()
    tz = AppSettings.load().tz
    if kind == "expenses":
        rows = Expense.objects.filter(day__range=(start, end)).select_related("category").order_by("-day", "-id")
        if search:
            rows = rows.filter(note__icontains=search) | rows.filter(category__name__icontains=search)
        data = [{"day": e.day.isoformat(), "amount": fmt.money(e.amount_base), "category": e.category.name,
                 "note": e.note, "recurring": e.recurring_id is not None} for e in rows[:MAX_ROWS]]
    elif kind == "workouts":
        rows = Workout.objects.filter(day__range=(start, end)).order_by("-day", "-id")
        if search:
            rows = rows.filter(notes__icontains=search) | rows.filter(type__icontains=search)
        data = [{"day": w.day.isoformat(), "type": w.type, "duration": fmt.minutes(w.duration_min) if w.duration_min else None,
                 "notes": w.notes} for w in rows[:MAX_ROWS]]
    elif kind == "sleep":
        rows = SleepSession.objects.filter(day__range=(start, end)).order_by("-day", "-id")
        data = [{"wake_day": s.day.isoformat(), "bedtime": fmt.clock(s.bedtime, tz) if s.bedtime else None,
                 "woke": fmt.clock(s.wake_time, tz) if s.wake_time else None,
                 "duration": fmt.minutes(s.duration_min) if s.duration_min is not None else None} for s in rows[:MAX_ROWS]]
    elif kind == "notes":
        rows = DayNote.objects.filter(day__range=(start, end)).order_by("-day")
        if search:
            rows = rows.filter(note__icontains=search)
        data = [{"day": n.day.isoformat(), "note": n.note} for n in rows[:MAX_ROWS]]
    elif kind == "tags":
        rows = DayTag.objects.filter(day__range=(start, end)).select_related("tag").order_by("-day")
        if search:
            rows = rows.filter(tag__slug__icontains=search)
        data = [{"day": t.day.isoformat(), "tag": t.tag.slug} for t in rows[:MAX_ROWS]]
    else:
        raise ToolError("kind must be one of expenses, workouts, sleep, notes, tags.")
    return {"kind": kind, "rows_returned": len(data), "more_exist": rows.count() > len(data), "rows": data}


def habit_streaks(args):
    today = days.today()
    out = []
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT):
        current, longest = metrics.habit_streaks(t, today)
        out.append({"habit": t.name, "metric": f"tracker:{t.slug}", "current_streak_days": current,
                    "longest_streak_days": longest})
    return {"as_of": today.isoformat(), "habits": out}


FUNCTIONS = {f.__name__: f for f in (get_series, summarize, compare_periods, correlation, find_days,
                                     weekday_averages, expense_breakdown, list_entries, habit_streaks)}

_DAY = {"type": "string", "description": "YYYY-MM-DD"}
_METRIC = {"type": "string", "description": "A metric key from the list in the instructions."}


def _tool(name, description, properties):
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "properties": properties, "required": list(properties),
                             "additionalProperties": False}}


TOOLS = [
    _tool("summarize", "Average, total (where it makes sense), highest and lowest day, and how many days were logged, "
          "for one metric over a date range. Use this for most 'how much / how often / on average' questions.",
          {"metric": _METRIC, "start": _DAY, "end": _DAY}),
    _tool("compare_periods", "The same summary for two date ranges, plus the percentage change from A to B. "
          "Use this for 'this month vs last month' questions instead of working out the difference yourself.",
          {"metric": _METRIC, "start_a": _DAY, "end_a": _DAY, "start_b": _DAY, "end_b": _DAY}),
    _tool("get_series", "The value for every day in a range. Use only when the day-by-day detail is needed.",
          {"metric": _METRIC, "start": _DAY, "end": _DAY}),
    _tool("find_days", "The days where a metric passes a threshold, and how many there are. Durations and clock "
          "times are compared in minutes (7 hours = 420; bedtimes count minutes after noon; wake times after midnight).",
          {"metric": _METRIC, "comparison": {"type": "string", "enum": list(_COMPARE)},
           "value": {"type": "number"}, "start": _DAY, "end": _DAY}),
    _tool("weekday_averages", "Average of a metric for each day of the week over a range.",
          {"metric": _METRIC, "start": _DAY, "end": _DAY}),
    _tool("correlation", "How two metrics move together over a range (Spearman rank correlation). lag_days=1 "
          "compares metric_x on one day with metric_y on the next day; sleep belongs to the day of waking, so use "
          "lag_days=1 for 'does X affect that night's sleep'.",
          {"metric_x": _METRIC, "metric_y": _METRIC, "start": _DAY, "end": _DAY, "lag_days": {"type": "integer"}}),
    _tool("expense_breakdown", "Total spending and spending per category over a range.", {"start": _DAY, "end": _DAY}),
    _tool("list_entries", "Individual logged rows (at most 100) of one kind, optionally filtered by text. "
          "Pass an empty string for search to get everything.",
          {"kind": {"type": "string", "enum": ["expenses", "workouts", "sleep", "notes", "tags"]},
           "start": _DAY, "end": _DAY, "search": {"type": "string"}}),
    _tool("habit_streaks", "Current and longest streak for every habit.", {}),
]


def system_prompt() -> str:
    lines = [f"- {m.key}: {m.name} ({m.kind}; shown as {m.fmt})" for m in _all_metrics()]
    return f"""You answer one person's questions about their own life-tracking data: sleep, mood, energy, coffee, alcohol, screen time, exercise, habits and spending. You can only see the data through the query tools.

Metrics you can query:
{chr(10).join(lines)}

How to answer:
- Look things up with the tools before answering; do not answer from assumption. Ask for exactly the range the question implies. The message tells you today's date and when the data starts.
- Take every figure in your answer from a tool result, written as it appears there. Do not do arithmetic on results yourself: if you need a total, an average, a difference or a count, there is a tool that returns it (summarize, compare_periods, find_days). If no tool can give a figure, say so instead of estimating.
- A day with no value was not logged. Do not count it as zero, and mention it when a range is only partly logged and that affects the answer.
- A correlation is a tendency in this person's history, not proof that one thing causes another. Say how many days it rests on.
- Answer in a few plain sentences, leading with the answer. No headings or tables. If the question is not about their data, say what you can help with."""


@dataclass
class Answer:
    text: str = ""
    steps: list = field(default_factory=list)  # [{"tool", "input", "ok"}]
    unverified: list = field(default_factory=list)  # numbers no query returned
    error: str = ""


def run_tool(name: str, args) -> tuple[str, bool]:
    function = FUNCTIONS.get(name)
    if function is None:
        return f"Unknown tool {name!r}.", False
    try:
        return json.dumps(function(args or {}), ensure_ascii=False), True
    except ToolError as e:
        return str(e), False


def ask(question: str, client=None) -> Answer:
    question = (question or "").strip()
    if not question:
        return Answer(error="Ask a question about your data.")
    if not (django_settings.ANTHROPIC_API_KEY or client):
        return Answer(error="Ask my data needs a Claude API key (ANTHROPIC_API_KEY in .env).")
    client = client or anthropic.Anthropic(api_key=django_settings.ANTHROPIC_API_KEY)
    today = days.today()
    first = metrics.first_day()
    context = (f"Today is {today:%A} {today.isoformat()}. Yesterday was {(today - timedelta(days=1)).isoformat()}. "
               f"The data starts on {first.isoformat() if first else 'no data yet'}.")
    messages = [{"role": "user", "content": f"{context}\n\nQuestion: {question}"}]
    answer = Answer()
    seen = context + question

    for _round in range(MAX_ROUNDS):
        try:
            response = client.with_options(timeout=90.0, max_retries=1).messages.create(
                model=django_settings.INSIGHTS_MODEL, max_tokens=8000, system=system_prompt(), tools=TOOLS,
                output_config={"effort": "medium"}, messages=messages,
            )
        except anthropic.AuthenticationError:
            return Answer(error="The Anthropic API key was rejected.")
        except anthropic.RateLimitError:
            return Answer(error="Rate limited by the API. Try again in a moment.")
        except anthropic.APIStatusError as e:
            return Answer(error=f"The API returned an error ({e.status_code}).")
        except anthropic.APIConnectionError:
            return Answer(error="Could not reach the API.")

        if response.stop_reason == "refusal":
            return Answer(error="Claude declined to answer that.", steps=answer.steps)
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            answer.text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens" or not answer.text:
                return Answer(error="The answer was cut off. Try a narrower question.", steps=answer.steps)
            answer.unverified = sorted(numbers_in(answer.text) - numbers_in(seen))
            return answer

        # Thinking and tool_use blocks go back exactly as received.
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            output, ok = run_tool(call.name, call.input)
            seen += output
            answer.steps.append({"tool": call.name, "input": call.input, "ok": ok})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output, "is_error": not ok})
        messages.append({"role": "user", "content": results})

    return Answer(error="That took too many lookups. Try a more specific question.", steps=answer.steps)
