"""The streak card on Home: one encouraging fact, computed here and handed
to the page as finished strings (numbers come from Python only).

Candidates, best first:
1. a habit's best week: last week (Mon–Sun) had more done days than any of
   the 7 weeks before it, and at least 3;
2. a running habit streak of 3+ days;
3. a weekly goal met last week;
4. how many days of last week had anything logged.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from tracking import fmt, metrics
from tracking.models import Goal, Tracker

LOOKBACK_WEEKS = 7


@dataclass(frozen=True)
class Highlight:
    label: str
    value: str
    note: str
    habit: str = ""  # slug of a habit the card's button can tick for today
    done_today: bool = False


def _week(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]


def _done(series: dict, days) -> int:
    return sum(1 for d in days if (series.get(d) or 0) >= 1)


def highlight(today: date) -> Highlight | None:
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    first = last_monday - timedelta(days=7 * LOOKBACK_WEEKS)
    habits = list(Tracker.objects.filter(is_active=True, kind=Tracker.Kind.HABIT))

    best = []
    streaks = []
    for t in habits:
        series = metrics.series(f"tracker:{t.slug}", first, today)
        done_today = (series.get(today) or 0) >= 1
        last = _done(series, _week(last_monday))
        earlier = [_done(series, _week(first + timedelta(days=7 * i))) for i in range(LOOKBACK_WEEKS)]
        logged_before = any(series.get(d) is not None for d in series if d < last_monday)
        if last >= 3 and logged_before and last > max(earlier):
            best.append((last, t, done_today))
        current, _ = metrics.habit_streaks(t, today)
        if current >= 3:
            streaks.append((current, t, done_today))

    if best:
        n, t, done_today = max(best, key=lambda x: x[0])
        return Highlight(f"{t.name} last week", f"{n} of 7",
                         "Your best week yet. " + ("Done today too." if done_today else "One today keeps it going."),
                         habit=t.slug, done_today=done_today)
    if streaks:
        n, t, done_today = max(streaks, key=lambda x: x[0])
        return Highlight(f"{t.name} streak", f"{n} days",
                         "Done today too." if done_today else "Tick it today to keep it going.",
                         habit=t.slug, done_today=done_today)

    for goal in Goal.objects.filter(is_active=True, period="week"):
        progress = metrics.goal_progress(goal, last_monday + timedelta(days=6))
        if progress and progress["met"]:
            m = progress["metric"]
            return Highlight("Goal met last week", f"{fmt.value(m.fmt, progress['value'])} of {fmt.value(m.fmt, goal.target)}",
                             f"{goal.name}. Same again this week?")

    logged = sum(1 for d in _week(last_monday) if metrics.logged_share(d) > 0)
    if not logged:
        return None
    return Highlight("Logged last week", f"{logged} of 7 days",
                     "Every day counts, even a quick one." if logged < 7 else "Every single day. Nice.")
