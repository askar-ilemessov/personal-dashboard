from datetime import date, time
from decimal import Decimal

import pytest

from conftest import local
from parsing.describe import describe
from parsing.grammar import parse, parse_clock, parse_duration
from tracking import logbook, metrics
from tracking.logbook import DayNoteLog, DayTagLog, ExpenseLog, SleepLog, TrackerLog, WorkoutLog
from tracking.models import ExchangeRate, Tracker

NOW = local(2026, 11, 14, 9, 0)  # Saturday morning
D = date(2026, 11, 14)


def one(text, now=NOW):
    result = parse(text, now=now)
    assert result.errors == [], result.errors
    assert len(result.intents) == 1, result.intents
    return result.intents[0]


@pytest.mark.parametrize(
    "text,minutes",
    [("3h20", 200), ("3h 20m", 200), ("3h", 180), ("1.5h", 90), ("45m", 45), ("45 min", 45),
     ("200", 200), ("3:20", 200), ("2 hours", 120), ("", None), ("soon", None), ("h", None)],
)
def test_parse_duration(text, minutes):
    assert parse_duration(text) == minutes


@pytest.mark.parametrize(
    "text,expected",
    [("7", time(7)), ("7:10", time(7, 10)), ("7am", time(7)), ("7:10pm", time(19, 10)), ("12am", time(0)),
     ("12pm", time(12)), ("23:30", time(23, 30)), ("25:00", None), ("13pm", None), ("noon", None)],
)
def test_parse_clock(text, expected):
    assert parse_clock(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("coffee", TrackerLog(tracker="coffee")),
        ("Coffee", TrackerLog(tracker="coffee")),
        ("coffee 2", TrackerLog(tracker="coffee", value="2")),
        ("2 coffees", TrackerLog(tracker="coffee", value="2")),
        ("coffee x2", TrackerLog(tracker="coffee", value="2")),
        ("coffee =1", TrackerLog(tracker="coffee", value="1", mode="set")),
        ("coffee flat white", TrackerLog(tracker="coffee", note="flat white")),
        ("beer", TrackerLog(tracker="alcohol")),
        ("3 drinks", TrackerLog(tracker="alcohol", value="3")),
        ("no alcohol", TrackerLog(tracker="alcohol", value=0, mode="set")),
        ("alcohol 0", TrackerLog(tracker="alcohol", value="0", mode="set")),
        ("junk food", TrackerLog(tracker="junk")),
        ("2 junk food", TrackerLog(tracker="junk", value="2")),
        ("screen 3h20", TrackerLog(tracker="screen", value=200)),
        ("screen time 3h 20m", TrackerLog(tracker="screen", value=200)),
        ("screen 185", TrackerLog(tracker="screen", value=185)),
        ("mood 4", TrackerLog(tracker="mood", value="4")),
        ("energy 2 rough night", TrackerLog(tracker="energy", value="2", note="rough night")),
        ("read", TrackerLog(tracker="read")),
        ("meditate done", TrackerLog(tracker="meditate", value=1)),
        ("skip stretch", TrackerLog(tracker="stretch", value=0, mode="set")),
        ("vitamins no", TrackerLog(tracker="vitamins", value=0, mode="set")),
    ],
)
def test_trackers(text, expected):
    assert one(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("spent 12.5 food", ExpenseLog(amount="12.5", category="food")),
        ("12.5 food", ExpenseLog(amount="12.5", category="food")),
        ("$12.50 on groceries milk", ExpenseLog(amount="12.50", category="food", note="milk")),
        ("spent 12,50 dining ramen", ExpenseLog(amount="12.50", category="eating-out", note="ramen")),
        ("18 transport uber home", ExpenseLog(amount="18", category="transport", note="uber home")),
        ("paid 40 usd dining", ExpenseLog(amount="40", currency="USD", category="eating-out")),
        ("spent 40usd for eating out tacos", ExpenseLog(amount="40", currency="USD", category="eating-out", note="tacos")),
        ("spent 49 gym", ExpenseLog(amount="49", category="health")),
    ],
)
def test_expenses(text, expected):
    ExchangeRate.objects.create(currency="USD", rate=Decimal("1.37"))
    assert one(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("gym legs 60", WorkoutLog(type="legs", duration_min=60)),
        ("legs 60", WorkoutLog(type="legs", duration_min=60)),
        ("gym", WorkoutLog(type="other")),
        ("workout push 1h 15m", WorkoutLog(type="push", duration_min=75)),
        ("cardio 35m easy run", WorkoutLog(type="cardio", duration_min=35, notes="easy run")),
        ("gym 45 climbing", WorkoutLog(type="other", duration_min=45, notes="climbing")),
        ("Pull", WorkoutLog(type="pull")),
    ],
)
def test_workouts(text, expected):
    assert one(text) == expected


def test_sleep_now_and_clock_times():
    assert one("woke") == SleepLog(wake_time=NOW)
    assert one("slept") == SleepLog(bedtime=NOW)
    assert one("woke 7:10") == SleepLog(wake_time=local(2026, 11, 14, 7, 10))
    assert one("woke up at 7am") == SleepLog(wake_time=local(2026, 11, 14, 7, 0))
    # A time that hasn't happened yet today means yesterday...
    assert one("slept 23:30") == SleepLog(bedtime=local(2026, 11, 13, 23, 30))
    # ...unless it's only minutes away.
    assert one("woke 9:10") == SleepLog(wake_time=local(2026, 11, 14, 9, 10))
    # Typed at night, a bedtime of a few minutes ago is tonight.
    assert one("bed 23:30", now=local(2026, 11, 14, 23, 40)) == SleepLog(bedtime=local(2026, 11, 14, 23, 30))
    assert one("slept 1:15", now=local(2026, 11, 15, 8, 0)) == SleepLog(bedtime=local(2026, 11, 15, 1, 15))


@pytest.mark.parametrize("text", ["slept 23:30-7:10", "slept 23:30 to 7:10", "sleep 11:30pm - 7:10am", "slept 23:30 woke 7:10"])
def test_whole_night(text):
    assert one(text) == SleepLog(bedtime=local(2026, 11, 13, 23, 30), wake_time=local(2026, 11, 14, 7, 10))


def test_whole_night_after_midnight_bedtime():
    assert one("slept 1:30-8") == SleepLog(bedtime=local(2026, 11, 14, 1, 30), wake_time=local(2026, 11, 14, 8, 0))


def test_notes_and_tags():
    assert one("note: long day, good dinner and a walk") == DayNoteLog(note="long day, good dinner and a walk")
    assert one("#sick #travel") == DayTagLog(tags=["sick", "travel"])
    assert one("tag deadline") == DayTagLog(tags=["deadline"])


def test_several_things_at_once():
    result = parse("coffee, mood 4 and energy 3; spent 12.5 food milk, gym legs 60", now=NOW)
    assert result.ok
    assert result.intents == [
        TrackerLog(tracker="coffee"),
        TrackerLog(tracker="mood", value="4"),
        TrackerLog(tracker="energy", value="3"),
        ExpenseLog(amount="12.5", category="food", note="milk"),
        WorkoutLog(type="legs", duration_min=60),
    ]


def test_backfill_prefixes():
    assert one("yesterday coffee 3") == TrackerLog(tracker="coffee", value="3", day=date(2026, 11, 13))
    assert one("2026-11-01 spent 20 food") == ExpenseLog(amount="20", category="food", day=date(2026, 11, 1))
    assert one("yesterday woke 7:10") == SleepLog(wake_time=local(2026, 11, 13, 7, 10))
    assert one("2026-11-10 slept 23:30") == SleepLog(bedtime=local(2026, 11, 9, 23, 30), day=date(2026, 11, 10))
    assert one("2026-11-10 slept 0:45-8") == SleepLog(
        bedtime=local(2026, 11, 10, 0, 45), wake_time=local(2026, 11, 10, 8, 0)
    )
    # Before the 04:00 boundary "yesterday" is relative to the logical day.
    assert one("yesterday coffee", now=local(2026, 11, 14, 2, 0)).day == date(2026, 11, 12)


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("mood", "needs a value"),
        ("screen", "needs a value"),
        ("screen lots", "duration"),
        ("mood great", "needs a number"),
        ("spent 12 unicorns", "Unknown category"),
        ("spent 12", "Which category"),
        ("spent lots food", "Expenses look like"),
        ("asdf qwer", "Couldn't understand"),
        ("woke sometime", "as a time"),
        ("yesterday woke", "Add a time"),
        ("no unicorns", "Couldn't understand"),
        ("12.5", "Couldn't understand"),
    ],
)
def test_errors_are_explained(text, fragment):
    result = parse(text, now=NOW)
    assert not result.ok and fragment in result.errors[0]


def test_one_bad_clause_blocks_the_whole_input():
    result = parse("coffee, blargh", now=NOW)
    assert not result.ok and len(result.intents) == 1 and len(result.errors) == 1


def test_empty_input():
    assert not parse("   ").ok and parse("").errors == []


def test_new_tracker_is_parseable_without_code():
    Tracker.objects.create(slug="water", name="Water", kind="counter", aliases=["glass of water"])
    assert one("glass of water") == TrackerLog(tracker="water")
    assert one("3 waters") == TrackerLog(tracker="water", value="3")


def test_parsed_intents_apply_cleanly():
    result = parse("2 coffees, no alcohol, screen 3h20, mood 4, read, slept 23:30-7:10, legs 60, 12.5 food", now=NOW)
    logbook.apply(result.intents, source="web", now=NOW)
    assert metrics.value("tracker:coffee", D) == 2
    assert metrics.value("tracker:alcohol", D) == 0
    assert metrics.value("tracker:screen", D) == 200
    assert metrics.value("sleep.duration", D) == 460
    assert metrics.value("expenses.total", D) == Decimal("12.50")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("coffee", "Coffee +1"),
        ("no alcohol", "Alcohol = 0"),
        ("screen 3h20", "Screen time: 3h 20m"),
        ("mood 4", "Mood: 4"),
        ("skip read", "Read 20 min: not done"),
        ("spent 12.5 food milk", "$12.50 · Groceries · milk"),
        ("slept 23:30-7:10", "Slept 23:30 → 07:10 (7h 40m)"),
        ("woke 7:10", "Woke up Sat 07:10"),
        ("legs 60", "Workout: Legs · 1h"),
        ("#sick", "Tags: #sick"),
        ("yesterday coffee 2", "Coffee +2 (Nov 13)"),
    ],
)
def test_describe(text, expected):
    assert describe(one(text)) == expected
