from datetime import date, timedelta

from insights.highlights import highlight
from tracking import logbook
from tracking.logbook import TrackerLog, WorkoutLog

MON = date(2026, 11, 23)
LAST_MON = MON - timedelta(days=7)


def log(slug, day, value=1):
    logbook.apply([TrackerLog(tracker=slug, value=value, day=day)], source="web")


def test_nothing_logged_means_no_card(db):
    assert highlight(MON) is None


def test_best_week_for_a_habit(db):
    for i in range(2):  # two weeks ago: 2 days
        log("read", LAST_MON - timedelta(days=7 - i))
    for i in range(5):  # last week: 5 days, a record
        log("read", LAST_MON + timedelta(days=i))
    h = highlight(MON)
    assert (h.label, h.value, h.habit) == ("Read 20 min last week", "5 of 7", "read")
    assert h.note == "Your best week yet. One today keeps it going." and not h.done_today
    log("read", MON)
    assert highlight(MON).note == "Your best week yet. Done today too."


def test_a_first_week_is_not_called_a_record(db):
    for i in range(5):  # Mon–Fri last week, nothing logged before: no "best week"
        log("read", LAST_MON + timedelta(days=i))
    h = highlight(MON)  # and the weekend gap ended the streak
    assert (h.label, h.value) == ("Logged last week", "5 of 7 days")


def test_running_streak(db):
    for i in range(1, 5):
        log("meditate", MON - timedelta(days=i))
    h = highlight(MON)
    assert (h.label, h.value) == ("Meditate streak", "4 days")
    assert h.note == "Tick it today to keep it going."


def test_weekly_goal_met(db):
    for i in range(4):
        logbook.apply([WorkoutLog(type="legs", duration_min=40, day=LAST_MON + timedelta(days=i))], source="web")
    h = highlight(MON)
    assert (h.label, h.value) == ("Goal met last week", "4 of 4")


def test_fallback_counts_logged_days(db):
    log("coffee", LAST_MON + timedelta(days=2), 2)
    log("coffee", LAST_MON + timedelta(days=3), 1)
    h = highlight(MON)
    assert (h.label, h.value) == ("Logged last week", "2 of 7 days")
