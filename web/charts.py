"""Server-rendered SVG (sparklines, habit heatmap, month calendar) and the
JSON payloads that static/web/app.js turns into Chart.js charts."""

from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from tracking import fmt
from tracking.metrics import Metric

WEEKLY_ABOVE_DAYS = 120  # longer ranges are charted per week


def sparkline(values: list, width: int = 104, height: int = 30):
    """Seven-ish thin bars. Unlogged days are a small hollow tick, so a gap
    never reads as zero. The last bar (today) is emphasised."""
    n = len(values)
    logged = [float(v) for v in values if v is not None]
    top = max(logged, default=0) or 1
    slot = width / n
    bar = min(10, slot - 2)
    parts = []
    for i, v in enumerate(values):
        x = i * slot + (slot - bar) / 2
        cls = "spark-today" if i == n - 1 else "spark-bar"
        if v is None:
            parts.append(format_html(
                '<rect class="spark-gap" x="{}" y="{}" width="{}" height="2" rx="1"/>',
                f"{x:.1f}", height - 2, f"{bar:.1f}"))
        else:
            h = max(2.0, float(v) / top * (height - 2))
            parts.append(format_html(
                '<rect class="{}" x="{}" y="{}" width="{}" height="{}" rx="2"/>',
                cls, f"{x:.1f}", f"{height - h:.1f}", f"{bar:.1f}", f"{h:.1f}"))
    return format_html(
        '<svg class="spark" viewBox="0 0 {} {}" width="{}" height="{}" aria-hidden="true">{}</svg>',
        width, height, width, height, mark_safe("".join(parts)))


def heatmap(values: dict, today: date, weeks: int = 26, cell: int = 12, gap: int = 3):
    """GitHub-style grid, one column per week (Monday on top).
    values: day -> 1 (done), 0 (not done) or None (not logged)."""
    start = today - timedelta(days=today.weekday() + 7 * (weeks - 1))
    step = cell + gap
    cells = []
    months = []
    for w in range(weeks):
        monday = start + timedelta(days=7 * w)
        if monday.day <= 7:
            months.append(format_html('<text class="heat-label" x="{}" y="9">{}</text>', w * step, f"{monday:%b}"))
        for d in range(7):
            day = monday + timedelta(days=d)
            if day > today:
                continue
            v = values.get(day)
            state, word = ("none", "not logged") if v is None else ("done", "done") if v >= 1 else ("miss", "not done")
            cells.append((
                reverse("web:day", args=[day.isoformat()]), state, w * step, 14 + d * step, cell, cell,
                f"{day:%a, %b} {day.day}: {word}",
            ))
    body = format_html_join(
        "", '<a href="{}"><rect class="heat heat-{}" x="{}" y="{}" width="{}" height="{}" rx="3">'
        "<title>{}</title></rect></a>", cells)
    width, height = weeks * step - gap, 14 + 7 * step - gap
    return format_html(
        '<svg class="heatmap" viewBox="0 0 {} {}" width="{}" height="{}" role="img" aria-label="Habit history">{}{}</svg>',
        width, height, width, height, mark_safe("".join(months)), body)


def _weekly(metric: Metric, series: dict) -> tuple[list, list]:
    buckets: dict[date, list] = {}
    for day, v in series.items():
        buckets.setdefault(day - timedelta(days=day.weekday()), []).append(v)
    labels, data = [], []
    for monday, vals in buckets.items():
        logged = [v for v in vals if v is not None]
        labels.append(monday)
        if not logged:
            data.append(None)
        else:
            total = sum(logged, Decimal(0))
            data.append(total if metric.agg == "sum" else total / len(logged))
    return labels, data


def payload(metric: Metric, current: dict, previous: dict | None = None, goal=None, compact: bool = False) -> dict:
    """Chart description for app.js. One measure, one axis; the previous
    period is the same measure drawn as a muted comparison series."""
    weekly = len(current) > WEEKLY_ABOVE_DAYS or metric.kind == "habit"
    if weekly:
        labels, data = _weekly(metric, current)
        prev = _weekly(metric, previous)[1] if previous else None
    else:
        labels, data = list(current), list(current.values())
        prev = list(previous.values()) if previous else None
    value_fmt = metric.agg_fmt if weekly else metric.fmt
    return {
        "type": "line" if metric.kind in ("scale", "duration") else "bar",
        "labels": [f"{d:%b} {d.day}" for d in labels],
        "data": [None if v is None else round(float(v), 2) for v in data],
        "previous": None if prev is None else [None if v is None else round(float(v), 2) for v in prev[: len(data)]],
        "fmt": "percent" if value_fmt == "percent" else value_fmt,
        "name": metric.name + (" (weekly)" if weekly and metric.kind != "habit" else ""),
        "goal": None if goal is None or weekly else {"value": float(goal.target), "label": goal.name},
        "compact": compact,
        "max": 1 if value_fmt == "percent" else None,
    }


def delta(current, previous) -> dict | None:
    """Change between two aggregates, for "vs previous period" lines."""
    if current is None or previous is None or previous == 0:
        return None
    change = float((current - previous) / previous)
    arrow = "▲" if change > 0.005 else "▼" if change < -0.005 else "＝"
    return {"arrow": arrow, "pct": f"{abs(change) * 100:.0f}%", "previous": previous}


def fmt_value(metric_fmt: str, value) -> str:
    return fmt.value(metric_fmt, value)
