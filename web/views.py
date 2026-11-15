"""Web UI. Views only read through tracking.metrics and write through
tracking.logbook; there is no business logic here."""

import calendar
import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from parsing import parser
from parsing.describe import describe
from parsing.grammar import parse, parse_duration
from parsing.models import Draft
from tracking import days, fmt, logbook, metrics
from tracking.logbook import DayNoteLog, DayTagLog, ExpenseLog, LogError, TrackerLog, WorkoutLog
from tracking.models import (
    AppSettings,
    DayNote,
    DayTag,
    Entry,
    Expense,
    ExpenseCategory,
    Goal,
    Operation,
    SleepSession,
    Tracker,
    Workout,
)

from . import charts
from .tables import TABLES

PAGE_SIZE = 50
RANGES = [("7d", "7 days", 7), ("30d", "30 days", 30), ("90d", "90 days", 90), ("1y", "1 year", 365)]
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
    """Selected range plus the equally long period before it."""
    key = request.GET.get("range", "30d")
    start = end = None
    if key == "custom":
        try:
            start, end = date.fromisoformat(request.GET["start"]), date.fromisoformat(request.GET["end"])
        except (KeyError, ValueError):
            key = "30d"
        else:
            if start > end:
                start, end = end, start
            end = min(end, today)
            start = min(start, end)
    if key != "custom":
        length = next((n for k, _, n in RANGES if k == key), None)
        if length is None:
            key, length = "30d", 30
        end, start = today, today - timedelta(days=length - 1)
    n = (end - start).days + 1
    params = {"range": key} | ({"start": start.isoformat(), "end": end.isoformat()} if key == "custom" else {})
    return {
        "key": key, "start": start, "end": end, "days": n, "options": RANGES, "params": params,
        "prev_start": start - timedelta(days=n), "prev_end": start - timedelta(days=1),
        "query": urlencode(params),
    }


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


def today(request):
    s = AppSettings.load()
    day = days.today(s)
    week_start, prev_start = day - timedelta(days=6), day - timedelta(days=13)
    daily_goals = {g.metric: g for g in Goal.objects.filter(is_active=True, period="day")}
    first = metrics.first_day()

    cards = []
    for m in metrics.catalog(habits=False):
        series = metrics.series(m.key, prev_start, day)
        current = {d: v for d, v in series.items() if d >= week_start}
        previous = metrics.comparable({d: v for d, v in series.items() if d < week_start}, first)
        now_avg = metrics.aggregate(m, current)
        before_avg = metrics.aggregate(m, previous) if previous else None
        value = series[day]
        goal = daily_goals.get(m.key)
        cards.append({
            "metric": m, "url": _metric_url(m), "value": fmt.value(m.fmt, value), "logged": value is not None,
            "spark": charts.sparkline(list(current.values())),
            "week": fmt.value(m.agg_fmt, now_avg), "week_label": "7-day total" if m.agg == "sum" else "7-day avg",
            "delta": charts.delta(now_avg, before_avg),
            "goal": goal, "goal_met": goal.is_met(value) if goal else None,
        })

    logged = dict(Entry.objects.filter(day=day).order_by("occurred_at", "id").values_list("tracker__slug", "value"))
    sums = {m.key.split(":", 1)[1]: metrics.value(m.key, day) for m in metrics.catalog() if m.kind == "counter"}
    trackers = list(Tracker.objects.filter(is_active=True))
    habits = []
    for t in (t for t in trackers if t.kind == Tracker.Kind.HABIT):
        current, longest = metrics.habit_streaks(t, day)
        habits.append(_control(t, logged.get(t.slug), day, day) | {"streak": current, "longest": longest})
    unlogged = [
        _control(t, sums.get(t.slug), day, day)
        for t in trackers if t.kind != Tracker.Kind.HABIT and t.slug not in logged
    ]
    return render(request, "web/today.html", {
        "nav": "today", "day": day, "cards": cards, "habits": habits, "unlogged": unlogged,
        "sleep_missing": not SleepSession.objects.filter(day=day, wake_time__isnull=False).exists(),
        "goals": [g for g in (metrics.goal_progress(g, day) for g in Goal.objects.filter(is_active=True)) if g],
        "budgets": metrics.budgets(day),
    })


def day_view(request, day):
    s = AppSettings.load()
    day, now = _parse_day(day), days.today(s)
    if day > now:
        return redirect("web:day", now.isoformat())

    trackers = list(Tracker.objects.filter(is_active=True))
    values = {t.slug: metrics.value(f"tracker:{t.slug}", day) for t in trackers}

    # Month calendar shaded by how many trackers were logged each day.
    first = day.replace(day=1)
    last = first.replace(day=calendar.monthrange(day.year, day.month)[1])
    per_day: dict[date, set] = {}
    for d, slug in Entry.objects.filter(day__range=(first, last)).values_list("day", "tracker__slug"):
        per_day.setdefault(d, set()).add(slug)
    weeks = []
    for week in calendar.Calendar().monthdatescalendar(day.year, day.month):
        weeks.append([
            None if d.month != day.month else {
                "date": d, "future": d > now, "selected": d == day, "today": d == now,
                "level": min(4, -(-4 * len(per_day.get(d, ())) // max(1, len(trackers)))),
                "count": len(per_day.get(d, ())),
            }
            for d in week
        ])
    note = DayNote.objects.filter(day=day).first()
    return render(request, "web/day.html", {
        "nav": "today", "day": day, "is_today": day == now, "today": now,
        "prev": day - timedelta(days=1), "next": day + timedelta(days=1) if day < now else None,
        "prev_month": first - timedelta(days=1), "next_month": last + timedelta(days=1) if last < now else None,
        "weeks": weeks, "tracker_total": len(trackers),
        "controls": [_control(t, values[t.slug], day, now) for t in trackers],
        "day_field": "" if day == now else day.isoformat(),
        "categories": ExpenseCategory.objects.filter(is_active=True),
        "workout_types": Workout.Type.choices,
        "note": note, "tags": DayTag.objects.filter(day=day).select_related("tag"),
        "sections": [
            ("Sleep", _mini_table("sleep", SleepSession.objects.filter(day=day), s)),
            ("Trackers", _mini_table("dayentry", Entry.objects.filter(day=day), s)),
            ("Workouts", _mini_table("workout", Workout.objects.filter(day=day), s)),
            ("Expenses", _mini_table("expense", Expense.objects.filter(day=day), s)),
        ],
    })


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
                "categories": ExpenseCategory.objects.filter(is_active=True)}

    categories = [(c.slug, c.name) for c in ExpenseCategory.objects.all()]
    filters = [{"name": "category", "label": "All categories", "choices": categories,
                "value": request.GET.get("category", "")}]
    return _detail(request, metrics.get("expenses.total"), "expense", TABLES["expense"].queryset(),
                   "expenses", extra, filters)


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
    s = AppSettings.load()
    now = days.today(s)
    weeks = 26
    start = now - timedelta(days=now.weekday() + 7 * (weeks - 1))
    month_ago = now - timedelta(days=29)
    logged_today = dict(Entry.objects.filter(day=now, tracker__kind="habit").values_list("tracker__slug", "value"))
    rows = []
    for t in Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT):
        series = metrics.tracker_series(t, start, now)
        current, longest = metrics.habit_streaks(t, now)
        recent = [v for d, v in series.items() if d >= month_ago]
        rows.append(_control(t, logged_today.get(t.slug), now, now) | {
            "heatmap": charts.heatmap(series, now, weeks), "streak": current, "longest": longest,
            "done_30": sum(1 for v in recent if v is not None and v >= 1),
            "logged_30": sum(1 for v in recent if v is not None),
        })
    return render(request, "web/habits.html", {"nav": "habits", "habits": rows, "today": now})


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


def insights(request):
    return render(request, "web/stub.html", {
        "nav": "insights", "title": "Insights",
        "text": "Weekly reviews, correlations, anomaly flags and Ask my data arrive in phase 5.",
    })


def settings_page(request):
    s = AppSettings.load()
    links = [
        ("Day boundary, timezone, reminder", "admin:tracking_appsettings_change", [1]),
        ("Trackers (habits, counters, scales)", "admin:tracking_tracker_changelist", []),
        ("Expense categories", "admin:tracking_expensecategory_changelist", []),
        ("Budgets", "admin:tracking_budget_changelist", []),
        ("Recurring expenses", "admin:tracking_recurringexpense_changelist", []),
        ("Exchange rates", "admin:tracking_exchangerate_changelist", []),
        ("Goals and limits", "admin:tracking_goal_changelist", []),
    ]
    return render(request, "web/settings.html", {
        "nav": "settings", "settings": s,
        "links": [(label, reverse(name, args=args)) for label, name, args in links],
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
            if not bed and not wake:
                raise LogError("Enter a bedtime, a wake time, or both.")
            text = f"slept {bed}-{wake}" if bed and wake else f"slept {bed}" if bed else f"woke {wake}"
            parsed = parse(f"{p['day']} {text}" if p.get("day") else text)
            if not parsed.ok:
                raise LogError(parsed.errors[0] if parsed.errors else "Couldn't read those times.")
            intents = parsed.intents
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
