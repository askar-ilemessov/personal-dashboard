"""Web UI. Views only read through tracking.metrics and write through
tracking.logbook; there is no business logic here."""

import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.conf import settings as django_settings
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from insights import ask, facts, review, stats
from insights.models import WeeklyReview
from parsing import parser
from parsing.describe import describe
from parsing.grammar import parse, parse_duration
from parsing.models import Draft
from tracking import days, fmt, logbook, metrics
from tracking.logbook import (
    DayNoteLog,
    DayTagLog,
    ExpenseLog,
    LogError,
    NoSpendLog,
    TrackerLog,
    WorkoutLog,
)
from tracking.models import (
    AppSettings,
    DayTag,
    Expense,
    ExpenseCategory,
    Goal,
    NoSpendDay,
    Operation,
    SleepSession,
    Tracker,
    Workout,
)

from . import charts
from .tables import TABLES

PAGE_SIZE = 50
RANGES = [("week", "Week", 7), ("30d", "30 days", 30), ("90d", "90 days", 90), ("1y", "1 year", 365)]
RANGE_KEYS = {k for k, _, _ in RANGES}
NAV = [
    ("today", "Today", "web:today"), ("trends", "Trends", "web:trends"), ("habits", "Habits", "web:habits"),
    ("expenses", "Expenses", "web:expenses"), ("sleep", "Sleep", "web:sleep"),
    ("exercise", "Exercise", "web:exercise"), ("insights", "Insights", "web:insights"),
    ("settings", "Settings", "web:settings"),
]


# --- helpers ---------------------------------------------------------------


def _events(**events) -> HttpResponse:
    """Empty response that fires client-side events (see app.js): refresh,
    toast, palette-close."""
    response = HttpResponse(status=200)
    response["HX-Trigger"] = json.dumps(events)
    return response


def _error(message) -> HttpResponse:
    return _events(toast={"message": str(message), "error": True})


def _commit(intents, raw_text="", **extra) -> HttpResponse:
    try:
        op = logbook.apply(intents, source="web", raw_text=raw_text)
        summary = "; ".join(describe(i) for i in intents)
    except LogError as e:
        return _error(e)
    return _events(refresh={}, toast={"message": summary, "op": op.pk}, **extra)


def _parse_day(text) -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise Http404("Bad date") from None


def _metric_url(metric) -> str:
    if metric.key.startswith("tracker:"):
        return reverse("web:tracker", args=[metric.key.split(":", 1)[1]])
    return reverse({"sleep": "web:sleep", "expenses": "web:expenses", "exercise": "web:exercise"}[metric.kind])


def _range(request, today: date) -> dict:
    """Selected range plus the period before it to compare with. "week" is a
    calendar week, Monday to Sunday (up to today), compared with the same
    days of the week before; the others are the last N days. With no choice
    in the URL, the account's default range applies (Settings)."""
    key = request.GET.get("range") or AppSettings.load().default_range
    if key == "7d":  # older links
        key = "week"
    start = end = None
    if key == "custom":
        try:
            start, end = date.fromisoformat(request.GET["start"]), date.fromisoformat(request.GET["end"])
        except (KeyError, ValueError):
            key = "week"
        else:
            if start > end:
                start, end = end, start
            end = min(end, today)
            start = min(start, end)
    if key == "week":
        try:
            anchor = min(date.fromisoformat(request.GET.get("week", "")), today)
        except ValueError:
            anchor = today
        start = anchor - timedelta(days=anchor.weekday())
        end = min(start + timedelta(days=6), today)
    elif key != "custom":
        length = next((n for k, _, n in RANGES if k == key), None)
        if length is None:
            key, length = "30d", 30
        end, start = today, today - timedelta(days=length - 1)
    n = (end - start).days + 1
    params = {"range": key}
    if key == "custom":
        params |= {"start": start.isoformat(), "end": end.isoformat()}
    elif key == "week" and start != today - timedelta(days=today.weekday()):
        params["week"] = start.isoformat()
    rng = {
        "key": key, "start": start, "end": end, "days": n, "options": RANGES, "params": params,
        "prev_start": start - timedelta(days=n), "prev_end": start - timedelta(days=1),
        "query": urlencode(params),
        "label": f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}",
    }
    if key == "week":
        sunday = start + timedelta(days=6)
        this_week = start == today - timedelta(days=today.weekday())
        rng |= {
            "prev_start": start - timedelta(days=7), "prev_end": end - timedelta(days=7),
            "week_number": start.isocalendar().week,
            "label": f"Week {start.isocalendar().week} · {start:%b} {start.day} – {sunday:%b} {sunday.day}"
                     + (" · so far" if this_week else ""),
            "prev_week": (start - timedelta(days=7)).isoformat(),
            "next_week": None if this_week else (start + timedelta(days=7)).isoformat(),
        }
    return rng


def _control(tracker: Tracker, value, day: date, today: date) -> dict:
    """Everything the one-tap control for a tracker needs."""
    scale = []
    if tracker.kind == Tracker.Kind.SCALE:
        lo, hi = int(tracker.min_value or 1), int(tracker.max_value or 5)
        scale = list(range(lo, hi + 1))
    return {
        "tracker": tracker, "value": value, "day": "" if day == today else day.isoformat(), "scale": scale,
        "display": fmt.value(metrics.for_tracker(tracker).fmt, value) if value is not None else None,
        "url": reverse("web:tracker", args=[tracker.slug]),
    }


def _table(request, table, queryset, s, base_url=None) -> dict:
    params = request.GET
    rows = table.order(table.filter(queryset, params), params)
    page = Paginator(rows, PAGE_SIZE).get_page(params.get("page"))
    keep = {k: v for k, v in params.items() if k != "page" and v}

    def link(**changes):
        return "?" + urlencode({**keep, **changes})

    columns = []
    for col in table.columns:
        active = col.sort and params.get("sort") == col.sort
        ascending = active and params.get("dir") == "asc"
        columns.append({
            "label": col.label, "align": col.align, "active": active, "ascending": ascending,
            "url": link(sort=col.sort, dir="desc" if ascending or not active else "asc") if col.sort else None,
        })
    return {
        "table": table, "columns": columns, "page": page, "total": page.paginator.count,
        "rows": [_row(table, r, s) for r in page],
        "prev_url": link(page=page.previous_page_number()) if page.has_previous() else None,
        "next_url": link(page=page.next_page_number()) if page.has_next() else None,
        "csv_url": reverse("web:export", args=[table.key]) + "?" + urlencode(keep),
        "q": params.get("q", ""), "keep": {k: v for k, v in keep.items() if k not in ("q", "category", "type")},
    }


def _row(table, row, s) -> dict:
    cells = table.cells(row, s)
    return {"table": table.key, "pk": row.pk,
            "cells": [{"value": v, "align": c.align} for v, c in zip(cells, table.columns, strict=True)]}


def _mini_table(table_key, queryset, s) -> dict:
    table = TABLES[table_key]
    rows = table.queryset().filter(pk__in=queryset.values("pk")).order_by(table.default_sort, *table.tiebreak)
    return {"table": table, "columns": [{"label": c.label, "align": c.align, "url": None} for c in table.columns],
            "rows": [_row(table, r, s) for r in rows], "mini": True}


# --- pages -----------------------------------------------------------------


def _first_steps(s, first) -> dict | None:
    """The getting-started card: shown until setup is finished and
    something has been logged."""
    if first is not None and not s.setup_step:
        return None
    from .setup_views import examples

    return {"name": s.user.first_name, "setup": s.setup_step, "logged": first is not None,
            "example": examples()[0]}


def _detail(request, metric, table_key, queryset, nav, extra=None, filters=None):
    s = AppSettings.load()
    now = days.today(s)
    rng = _range(request, now)
    current = metrics.series(metric.key, rng["start"], rng["end"])
    previous = metrics.comparable(
        metrics.series(metric.key, rng["prev_start"], rng["prev_end"]), metrics.first_day())
    agg = metrics.aggregate(metric, current)
    prev_agg = metrics.aggregate(metric, previous) if previous else None
    logged = [v for v in current.values() if v is not None]
    goal = Goal.objects.filter(is_active=True, metric=metric.key, period="day").first()

    stats = [{
        "label": metric.agg_label, "value": fmt.value(metric.agg_fmt, agg), "delta": charts.delta(agg, prev_agg),
        "previous": fmt.value(metric.agg_fmt, prev_agg),
    }]
    if metric.agg == "sum" and logged:
        stats.append({"label": "Per day", "value": fmt.value(metric.fmt, sum(logged, Decimal(0)) / len(logged))})
    if metric.kind not in ("expenses", "exercise"):
        stats.append({"label": "Days logged", "value": f"{len(logged)} of {rng['days']}"})
    if metric.kind not in ("habit", "expenses", "exercise") and logged:
        stats.append({"label": "Lowest", "value": fmt.value(metric.fmt, min(logged))})
    if metric.kind != "habit" and logged:
        stats.append({"label": "Highest", "value": fmt.value(metric.fmt, max(logged))})
    if goal and logged:
        met = sum(1 for v in logged if goal.is_met(v))
        stats.append({"label": goal.name, "value": f"{met} of {len(logged)} days"})

    table = TABLES[table_key]
    context = {
        "nav": nav, "metric": metric, "range": rng, "stats": stats, "has_previous": previous is not None,
        "chart": charts.payload(metric, current, previous, goal),
        "table": _table(request, table, queryset.filter(day__range=(rng["start"], rng["end"])), s),
        "filters": filters or [], "today": now,
    }
    context.update(extra(rng, s) if extra else {})
    return render(request, "web/detail.html", context)


def tracker_detail(request, slug):
    tracker = get_object_or_404(Tracker, slug=slug)
    s = AppSettings.load()
    now = days.today(s)

    def extra(rng, s):
        out = {"control": _control(tracker, metrics.value(f"tracker:{slug}", now), now, now)}
        if tracker.kind == Tracker.Kind.HABIT:
            current, longest = metrics.habit_streaks(tracker, now)
            out |= {"streak": current, "longest": longest}
        return out

    nav = "habits" if tracker.kind == Tracker.Kind.HABIT else "today"
    queryset = TABLES["entry"].queryset().filter(tracker=tracker)
    return _detail(request, metrics.for_tracker(tracker), "entry", queryset, nav, extra)


def expenses(request):
    def extra(rng, s):
        by_category = metrics.expenses_by_category(rng["start"], rng["end"])
        total = sum(by_category.values(), Decimal(0))
        names = dict(ExpenseCategory.objects.values_list("slug", "name"))
        breakdown = [
            {"slug": slug, "name": names.get(slug, slug), "amount": fmt.money(amount),
             "share": float(amount / total) if total else 0, "pct": f"{amount / total * 100:.0f}%" if total else ""}
            for slug, amount in sorted(by_category.items(), key=lambda kv: -kv[1])
        ]
        return {"breakdown": breakdown, "budgets": metrics.budgets(days.today(s)),
                "categories": ExpenseCategory.objects.filter(is_active=True), "spend": _spend_panel(s)}

    categories = [(c.slug, c.name) for c in ExpenseCategory.objects.all()]
    filters = [{"name": "category", "label": "All categories", "choices": categories,
                "value": request.GET.get("category", "")}]
    return _detail(request, metrics.get("expenses.total"), "expense", TABLES["expense"].queryset(),
                   "expenses", extra, filters)


def _spend_panel(s) -> dict:
    """Top of the Spending page: quick add, recent purchases to repeat, and
    this week's days, with the unlogged ones asking whether nothing was spent."""
    from collections import Counter

    today = days.today(s)
    recent = (Expense.objects.filter(day__gte=today - timedelta(days=60), recurring__isnull=True)
              .select_related("category").order_by("-occurred_at"))
    counts = Counter((e.category.slug, e.category.name, e.amount, e.currency) for e in recent[:200])
    presets = [{"slug": slug, "name": name, "amount": f"{amount.normalize():f}", "currency": currency,
                "label": f"{name} {fmt.money(amount, currency)}"}
               for (slug, name, amount, currency), _ in counts.most_common(4)]
    monday = today - timedelta(days=today.weekday())
    week = []
    for i in range(7):
        d = monday + timedelta(days=i)
        spent = Expense.objects.filter(day=d).exists()
        state = "future" if d > today else "spent" if spent else "none" if NoSpendDay.objects.filter(day=d).exists() \
            else "ask"
        week.append({"day": d, "state": state, "param": "" if d == today else d.isoformat(),
                     "total": fmt.money(metrics.value("expenses.total", d)) if spent else fmt.money(0)})
    return {"presets": presets, "week": week, "today": today, "yesterday": today - timedelta(days=1),
            "asks": sum(1 for w in week if w["state"] == "ask")}


def _mean_clock(moments, s, anchor_hour) -> str | None:
    """Average time of day. Times are measured from `anchor_hour` so that
    bedtimes on both sides of midnight average sensibly."""
    if not moments:
        return None
    offsets = []
    for m in moments:
        local = m.astimezone(s.tz)
        offsets.append(((local.hour - anchor_hour) % 24) * 60 + local.minute)
    mean = round(sum(offsets) / len(offsets)) + anchor_hour * 60
    return f"{mean // 60 % 24:02d}:{mean % 60:02d}"


def sleep(request):
    def extra(rng, s):
        sessions = SleepSession.objects.filter(day__range=(rng["start"], rng["end"]))
        bed = _mean_clock([x.bedtime for x in sessions if x.bedtime], s, 12)
        wake = _mean_clock([x.wake_time for x in sessions if x.wake_time], s, 0)
        return {"extra_stats": [{"label": "Average bedtime", "value": bed or "—"},
                                {"label": "Average wake-up", "value": wake or "—"}]}

    return _detail(request, metrics.get("sleep.duration"), "sleep", SleepSession.objects.all(), "sleep", extra)


def exercise(request):
    def extra(rng, s):
        rows = Workout.objects.filter(day__range=(rng["start"], rng["end"]))
        counts = {}
        for kind in rows.values_list("type", flat=True):
            counts[kind] = counts.get(kind, 0) + 1
        weeks = max(1, rng["days"] / 7)
        labels = dict(Workout.Type.choices)
        return {
            "extra_stats": [{"label": "Workouts", "value": str(rows.count())},
                            {"label": "Per week", "value": f"{rows.count() / weeks:.1f}"}],
            "type_counts": [(labels[k], n) for k, n in sorted(counts.items(), key=lambda kv: -kv[1])],
            "workout_types": Workout.Type.choices,
        }

    filters = [{"name": "type", "label": "All types", "choices": Workout.Type.choices,
                "value": request.GET.get("type", "")}]
    return _detail(request, metrics.get("exercise.minutes"), "workout", Workout.objects.all(), "exercise",
                   extra, filters)


def habits(request):
    """One card per habit: a big tick for today, the streak, this week's seven
    days (tap one to fix it) against the weekly target, and the last 26 weeks."""
    s = AppSettings.load()
    now = days.today(s)
    weeks = 26
    start = now - timedelta(days=now.weekday() + 7 * (weeks - 1))
    monday = now - timedelta(days=now.weekday())
    cards = []
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT):
        series = metrics.tracker_series(t, start, now)
        current, longest = metrics.habit_streaks(t, now)
        week = []
        for i in range(7):
            d = monday + timedelta(days=i)
            v = series.get(d) if d <= now else None
            state = "future" if d > now else "done" if v is not None and v >= 1 else "skip" if v is not None else "open"
            week.append({"day": d, "letter": "MTWTFSS"[i], "state": state, "today": d == now,
                         "param": "" if d == now else d.isoformat(), "next": 0 if state == "done" else 1})
        done_week = sum(1 for w in week if w["state"] == "done")
        target = t.weekly_target or 7
        today_value = series.get(now)
        cards.append({
            "tracker": t, "done": today_value is not None and today_value >= 1,
            "skipped": today_value is not None and today_value < 1,
            "streak": current, "longest": longest, "week": week, "done_week": done_week, "target": target,
            "met": done_week >= target, "left": max(target - done_week, 0),
            "heatmap": charts.heatmap(series, now, weeks), "url": reverse("web:tracker", args=[t.slug]),
        })
    done_today = sum(1 for c in cards if c["done"])
    return render(request, "web/habits.html", {"nav": "habits", "cards": cards, "today": now,
                                                "done_today": done_today, "targets": TARGETS})


TARGETS = [(7, "Every day"), (6, "6 a week"), (5, "5 a week"), (4, "4 a week"), (3, "3 a week"),
           (2, "2 a week"), (1, "Once a week")]


def trends(request):
    s = AppSettings.load()
    rng = _range(request, days.today(s))
    panels = []
    first = metrics.first_day()
    for m in metrics.catalog():
        current = metrics.series(m.key, rng["start"], rng["end"])
        previous = metrics.comparable(metrics.series(m.key, rng["prev_start"], rng["prev_end"]), first)
        agg = metrics.aggregate(m, current)
        prev_agg = metrics.aggregate(m, previous) if previous else None
        panels.append({
            "metric": m, "url": _metric_url(m) + "?" + rng["query"], "value": fmt.value(m.agg_fmt, agg),
            "label": m.agg_label, "delta": charts.delta(agg, prev_agg),
            "chart": charts.payload(m, current, compact=True),
        })
    return render(request, "web/trends.html", {"nav": "trends", "range": rng, "panels": panels})


def _flags(today: date) -> list[dict]:
    """Anomaly flags with links to where each can be inspected."""
    out = []
    for f in facts.anomalies(today):
        url = reverse("web:day", args=[f["day"].isoformat()])
        if f["metric"] and f["kind"] in ("sleep", "low", "budget"):
            url = _metric_url(metrics.get(f["metric"]))
        out.append(f | {"url": url})
    return out


def insights(request, week=None):
    s = AppSettings.load()
    now = days.today(s)
    if week:
        shown = get_object_or_404(WeeklyReview, week_start=_parse_day(week))
    else:
        shown = WeeklyReview.objects.first()
    this_week = facts.week_start(now)
    return render(request, "web/insights.html", {
        "nav": "insights", "review": shown, "reviews": WeeklyReview.objects.all()[:12],
        "this_week": this_week, "has_this_week": WeeklyReview.objects.filter(week_start=this_week).exists(),
        "flags": _flags(now), "llm": bool(django_settings.ANTHROPIC_API_KEY), "today": now,
    })


@require_POST
def insights_review(request):
    """Write (or rewrite) the review for the current week."""
    made = review.generate(force=True)
    who = "Claude" if made.written_by == "claude" else "the built-in template"
    return _events(refresh={}, toast={"message": f"Review written by {who}."})


@require_POST
def insights_ask(request):
    question = request.POST.get("question", "")
    return render(request, "web/partials/answer.html", {"question": question.strip(), "answer": ask.ask(question)})


def correlations(request):
    s = AppSettings.load()
    now = days.today(s)

    def card(rel):
        return {
            "rel": rel,
            "chart": {
                "type": "scatter", "name": f"{rel.x.name} vs {rel.y.name}",
                "points": [{"x": x, "y": y} for x, y in rel.pairs],
                "xname": rel.x.name, "yname": rel.y.name + (" (next day)" if rel.lag else ""),
                "xfmt": rel.x.fmt, "yfmt": rel.y.fmt,
            } if len(rel.pairs) >= 5 else None,
        }

    return render(request, "web/correlations.html", {
        "nav": "insights", "days": facts.HISTORY_DAYS, "min_pairs": stats.MIN_PAIRS,
        "asked": [card(r) for r in facts.correlations(now)],
        "others": [card(r) for r in facts.other_relationships(now)],
    })


# --- logging actions -------------------------------------------------------


@require_POST
def log(request):
    """Quick-log forms and one-tap controls."""
    p = request.POST
    when = {"day": _parse_day(p["day"])} if p.get("day") else {}
    kind = p.get("kind")
    try:
        if kind == "tracker":
            tracker = logbook.resolve_tracker(p.get("tracker", ""))
            value = p.get("value", "").strip() or None
            if tracker.kind == Tracker.Kind.DURATION:
                value = parse_duration(value or "")
                if value is None:
                    raise LogError("Enter a duration like 3h20 or 200.")
            intents = [TrackerLog(tracker=tracker.slug, value=value, mode=p.get("mode", "auto"), **when)]
        elif kind == "expense":
            intents = [ExpenseLog(amount=p.get("amount", ""), category=p.get("category", ""),
                                  currency=p.get("currency") or None, note=p.get("note", "").strip(), **when)]
        elif kind == "workout":
            text = p.get("duration", "").strip()
            duration = parse_duration(text) if text else None
            if text and duration is None:
                raise LogError("Enter a duration like 60 or 1h15.")
            intents = [WorkoutLog(type=p.get("type", ""), duration_min=duration,
                                  notes=p.get("notes", "").strip(), **when)]
        elif kind == "sleep":
            bed, wake = p.get("bed", "").strip(), p.get("wake", "").strip()
            span, now = p.get("range", "").strip(), p.get("now", "")
            if span:  # "23:30-7:10" from the Home log list
                text = f"slept {span}"
            elif now in ("bed", "wake"):  # "Going to bed" / "I'm up" buttons, today only
                text = "slept" if now == "bed" else "woke"
            elif bed or wake:
                text = f"slept {bed}-{wake}" if bed and wake else f"slept {bed}" if bed else f"woke {wake}"
            else:
                raise LogError("Enter a bedtime, a wake time, or both, like 23:30-7:10.")
            parsed = parse(f"{p['day']} {text}" if p.get("day") else text)
            if not parsed.ok:
                raise LogError(parsed.errors[0] if parsed.errors else "Couldn't read those times.")
            intents = parsed.intents
        elif kind == "nospend":
            intents = [NoSpendLog(**when)]
        elif kind == "note":
            intents = [DayNoteLog(note=p.get("note", ""), append=False, **when)]
        elif kind == "tags":
            tags = p.get("tags", "").replace(",", " ").replace("#", " ").split()
            intents = [DayTagLog(tags=tags, **when)]
        else:
            raise LogError("Unknown form.")
    except LogError as e:
        return _error(e)
    return _commit(intents)


def _navigation(text: str):
    """("go sleep") -> (label, url), or None if this isn't a navigation command."""
    if not text.lower().startswith(("go ", "goto ", "> ")):
        return None
    target = text.split(None, 1)[1].strip().lower() if " " in text else ""
    for key, label, name in NAV:
        if target and (key.startswith(target) or label.lower().startswith(target)):
            return label, reverse(name)
    return target, None


def _preview(request, result=None, **context):
    if result is not None:
        context |= {"lines": result.lines if result.ok else [], "errors": result.errors,
                    "ignored": result.ignored, "tier": result.tier, "text": result.text}
    return render(request, "web/partials/palette_preview.html", context)


@require_POST
def palette_parse(request):
    """Live preview while typing: grammar only, so no API call per keystroke."""
    text = request.POST.get("text", "").strip()
    if nav := _navigation(text):
        label, url = nav
        if url:
            return _preview(request, lines=[f"Go to {label}"], navigate=True, text=text)
        return _preview(request, errors=[f"No page called “{label}”."], text=text)
    result = parser.interpret(text, use_llm=False)
    ask_claude = bool(text) and not result.ok and parser.llm_enabled()
    return _preview(request, result, ask_claude=ask_claude)


@require_POST
def palette_confirm(request):
    text = request.POST.get("text", "").strip()
    close = {"palette-close": {}}
    if nav := _navigation(text):
        if not nav[1]:
            return _error(f"No page called “{nav[0]}”.")
        response = _events(**close)
        response["HX-Location"] = json.dumps({"path": nav[1], "target": "body"})
        return response

    # Second Enter on a Claude preview: save exactly what was shown.
    draft = Draft.objects.filter(pk=request.POST.get("draft") or 0, text=text, source="web").first()
    if draft:
        try:
            intents = parser.draft_intents(draft)
        except LogError as e:
            return _error(e)
        draft.delete()
        return _commit(intents, raw_text=text, **close)

    result = parser.interpret(text, use_llm=False)
    if result.ok:
        return _commit(result.intents, raw_text=text, **close)
    if not text:
        return _error("Type something to log.")
    if not parser.llm_enabled():
        return _error(result.errors[0])

    # The grammar couldn't read it: ask Claude, then show the result for confirmation.
    result = parser.interpret(text)
    context = {"draft": parser.save_draft(result, "web")} if result.ok else {}
    response = _preview(request, result, **context)
    response["HX-Retarget"] = "#palette-preview"
    response["HX-Reswap"] = "innerHTML"
    return response


@require_POST
def undo(request, pk):
    try:
        logbook.undo(get_object_or_404(Operation, pk=pk))
    except LogError as e:
        return _error(e)
    return _events(refresh={}, toast={"message": "Undone"})


# --- rows: inline edit, delete, export -------------------------------------


def _table_row(key, pk):
    table = TABLES.get(key)
    if table is None:
        raise Http404
    return table, get_object_or_404(table.queryset(), pk=pk)


def row(request, key, pk):
    table, obj = _table_row(key, pk)
    s = AppSettings.load()
    if request.GET.get("edit"):
        return render(request, "web/partials/row_edit.html", {
            "row": _row(table, obj, s), "fields": table.fields(obj, s), "span": len(table.columns) + 1})
    return render(request, "web/partials/row.html", {"row": _row(table, obj, s)})


@require_POST
def row_edit(request, key, pk):
    table, obj = _table_row(key, pk)
    s = AppSettings.load()
    try:
        op = logbook.edit(obj, source="web", **table.clean(obj, request.POST, s))
    except LogError as e:
        fields = table.fields(obj, s)
        for f in fields:  # keep what was typed
            f.value = request.POST.get(f.name, f.value)
        return render(request, "web/partials/row_edit.html", {
            "row": _row(table, obj, s), "fields": fields, "span": len(table.columns) + 1, "error": str(e)})
    obj = table.queryset().get(pk=pk)
    response = render(request, "web/partials/row.html", {"row": _row(table, obj, s)})
    response["HX-Trigger"] = json.dumps({"refresh": {}, "toast": {"message": "Saved", "op": op.pk}})
    return response


@require_POST
def row_delete(request, key, pk):
    if key == "tag":
        obj = get_object_or_404(DayTag, pk=pk)
    else:
        _, obj = _table_row(key, pk)
    try:
        op = logbook.remove(obj, source="web")
    except LogError as e:
        return _error(e)
    return _events(refresh={}, toast={"message": "Deleted", "op": op.pk})


@require_GET
def export(request, key):
    """CSV of the same rows the table shows (same range and filters, all pages)."""
    table = TABLES.get(key.removesuffix(".csv"))
    if table is None:
        raise Http404
    s = AppSettings.load()
    rng = _range(request, days.today(s))
    rows = table.filter(table.queryset().filter(day__range=(rng["start"], rng["end"])), request.GET)
    name = request.GET.get("tracker") or table.key
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}_{rng["start"]}_{rng["end"]}.csv"'
    writer = csv.writer(response)
    writer.writerow(table.csv_header)
    for obj in table.order(rows, request.GET):
        writer.writerow(table.csv_row(obj, s))
    return response
