from datetime import date, timedelta

from django.core.management import call_command

from tracking import days, logbook, metrics
from tracking.logbook import ExpenseLog, TrackerLog
from tracking.models import Entry, Expense, Goal, SleepSession, Tracker, Workout

D = date(2026, 11, 14)


def habit(pattern, end=D):
    """pattern: one char per day ending at `end`. x done, o not done, . unlogged."""
    for i, c in enumerate(reversed(pattern)):
        if c != ".":
            logbook.apply(
                [TrackerLog(tracker="read", value=int(c == "x"), day=end - timedelta(days=i))], source="web"
            )
    return metrics.habit_streaks(Tracker.objects.get(slug="read"), D)


def test_streak_counts_back_from_today():
    assert habit("xx.xxx") == (3, 3)


def test_unlogged_today_keeps_yesterdays_streak_alive():
    assert habit("xxxx", end=D - timedelta(days=1)) == (4, 4)


def test_missed_or_unlogged_day_breaks_the_streak():
    assert habit("xxxxo") == (0, 4)
    assert metrics.habit_streaks(Tracker.objects.get(slug="meditate"), D) == (0, 0)


def test_two_unlogged_days_break_the_streak():
    assert habit("xxx..") == (0, 3)


def test_a_gap_in_the_middle_splits_runs():
    assert habit("xxxxx.xx") == (2, 5)


def test_series_covers_every_day_in_range_with_none_for_gaps():
    logbook.apply([TrackerLog(tracker="coffee", value=2, day=D)], source="web")
    s = metrics.series("tracker:coffee", D - timedelta(days=2), D)
    assert list(s.values()) == [None, None, 2]


def test_expenses_by_category():
    logbook.apply(
        [ExpenseLog(amount=10, category="food", day=D), ExpenseLog(amount=5, category="food", day=D),
         ExpenseLog(amount=7, category="transport", day=D)],
        source="web",
    )
    assert metrics.expenses_by_category(D, D) == {"food": 15, "transport": 7}


def test_goal_is_met_distinguishes_unlogged():
    goal = Goal.objects.get(metric="tracker:coffee")
    assert goal.is_met(None) is None
    assert goal.is_met(2) is True and goal.is_met(3) is False
    assert Goal.objects.get(metric="sleep.duration").is_met(400) is False


def test_seed_produces_ninety_days_of_plausible_data():
    call_command("seed", days=90, verbosity=0)
    today = days.today()
    start = today - timedelta(days=90)
    assert Entry.objects.filter(source="seed").count() > 500
    assert Workout.objects.count() > 25 and SleepSession.objects.count() > 70
    assert Expense.objects.filter(recurring__isnull=False).count() >= 10
    assert Expense.objects.filter(currency="USD").exists()
    assert Expense.objects.order_by("day").first().day >= start

    sleep = [v for v in metrics.series("sleep.duration", start, today - timedelta(days=1)).values()]
    logged = [v for v in sleep if v is not None]
    assert None in sleep  # gaps exist
    assert all(120 <= v <= 720 for v in logged)
    alcohol = metrics.series("tracker:alcohol", start, today).values()
    assert 0 in alcohol and None in alcohol  # explicit zeros and unlogged days both present
    for slug in ("mood", "energy"):
        assert all(1 <= e.value <= 5 for e in Entry.objects.filter(tracker__slug=slug))
