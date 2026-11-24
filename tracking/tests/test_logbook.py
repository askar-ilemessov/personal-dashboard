from datetime import date, timedelta
from decimal import Decimal

import pytest

from conftest import local
from tracking import logbook, metrics
from tracking.logbook import (
    DayNoteLog,
    DayTagLog,
    ExpenseLog,
    LogError,
    SleepLog,
    TrackerLog,
    WorkoutLog,
)
from tracking.models import (
    DayNote,
    DayTag,
    Entry,
    ExchangeRate,
    Expense,
    ExpenseCategory,
    Operation,
    RecurringExpense,
    SleepSession,
    Tracker,
    Workout,
)

D = date(2026, 11, 14)
NOON = local(2026, 11, 14)


def log(*intents, **kw):
    kw.setdefault("now", NOON)
    return logbook.apply(intents, source="web", **kw)


def coffee(day=D):
    return metrics.value("tracker:coffee", day)


# --- counters, zero vs not logged -----------------------------------------


def test_counter_adds_up():
    assert coffee() is None  # not logged
    log(TrackerLog(tracker="coffee"))
    log(TrackerLog(tracker="coffee", value=2))
    assert coffee() == 3
    assert Entry.objects.count() == 2


def test_explicit_zero_is_not_the_same_as_unlogged():
    log(TrackerLog(tracker="alcohol", value=0))
    assert metrics.value("tracker:alcohol", D) == 0
    assert metrics.value("tracker:junk", D) is None
    assert "tracker:alcohol" not in metrics.unlogged(D)
    assert "tracker:junk" in metrics.unlogged(D)


def test_set_replaces_the_days_counter_entries():
    log(TrackerLog(tracker="coffee"), TrackerLog(tracker="coffee"), TrackerLog(tracker="coffee"))
    op = log(TrackerLog(tracker="coffee", value=1, mode="set"))
    assert coffee() == 1 and Entry.objects.count() == 1
    logbook.undo(op)
    assert coffee() == 3 and Entry.objects.count() == 3


def test_late_night_entry_counts_for_the_previous_day():
    log(TrackerLog(tracker="alcohol", at=local(2026, 11, 15, 1, 30)))
    assert metrics.value("tracker:alcohol", D) == 1
    assert metrics.value("tracker:alcohol", date(2026, 11, 15)) is None


def test_now_is_used_when_no_time_is_given():
    log(TrackerLog(tracker="coffee"), now=local(2026, 11, 15, 2, 0))
    e = Entry.objects.get()
    assert e.day == D and e.utc_offset_min == -300 and e.source == "web"


def test_backfill_by_day():
    log(TrackerLog(tracker="coffee", value=2, day=date(2026, 10, 1)))
    assert coffee(date(2026, 10, 1)) == 2 and coffee() is None


# --- single-value trackers -------------------------------------------------


def test_scale_keeps_one_value_per_day_and_undo_restores_the_old_one():
    log(TrackerLog(tracker="mood", value=2))
    op = log(TrackerLog(tracker="mood", value=4))
    assert Entry.objects.count() == 1
    assert metrics.value("tracker:mood", D) == 4
    logbook.undo(op)
    assert metrics.value("tracker:mood", D) == 2


@pytest.mark.parametrize(
    "intent",
    [
        TrackerLog(tracker="mood", value=6),
        TrackerLog(tracker="mood", value=0),
        TrackerLog(tracker="mood"),  # scale needs a value
        TrackerLog(tracker="mood", value=3, mode="add"),
        TrackerLog(tracker="coffee", value=-1),
        TrackerLog(tracker="coffee", value="lots"),
        TrackerLog(tracker="read", value=2),  # habit is 0/1
        TrackerLog(tracker="unicorns"),
        TrackerLog(tracker="coffee", mode="multiply"),
    ],
)
def test_invalid_tracker_logs_are_rejected(intent):
    with pytest.raises(LogError):
        log(intent)
    assert not Entry.objects.exists() and not Operation.objects.exists()


def test_habit_done_and_not_done():
    log(TrackerLog(tracker="read"))
    assert metrics.value("tracker:read", D) == 1
    log(TrackerLog(tracker="read", value=False))
    assert metrics.value("tracker:read", D) == 0


def test_trackers_resolve_by_alias_and_name_case_insensitively():
    log(TrackerLog(tracker="Beer"), TrackerLog(tracker="SCREEN TIME", value=200))
    assert metrics.value("tracker:alcohol", D) == 1
    assert metrics.value("tracker:screen", D) == 200


def test_a_new_tracker_needs_no_code():
    Tracker.objects.create(slug="water", name="Water", kind="counter", unit="glasses", aliases=["H2O"])
    log(TrackerLog(tracker="h2o", value=3))
    assert metrics.value("tracker:water", D) == 3
    assert "tracker:water" in metrics.unlogged(D + timedelta(days=1))


def test_inactive_trackers_are_not_loggable():
    Tracker.objects.filter(slug="junk").update(is_active=False)
    with pytest.raises(LogError):
        log(TrackerLog(tracker="junk"))


# --- atomicity and undo ----------------------------------------------------


def test_apply_is_all_or_nothing():
    with pytest.raises(LogError):
        log(TrackerLog(tracker="coffee"), ExpenseLog(amount=5, category="nope"))
    assert not Entry.objects.exists() and not Operation.objects.exists()


def test_empty_apply_is_rejected():
    with pytest.raises(LogError):
        logbook.apply([], source="web")


def test_undo_removes_everything_from_one_operation_once():
    op = log(
        TrackerLog(tracker="coffee"),
        ExpenseLog(amount="12.50", category="food"),
        WorkoutLog(type="legs", duration_min=60),
        DayTagLog(tags=["travel"]),
        DayNoteLog(note="hello"),
        raw_text="coffee, spent 12.5 food, gym legs 60",
    )
    assert len(op.changes) == 5 and op.raw_text.startswith("coffee")
    logbook.undo(op)
    for model in (Entry, Expense, Workout, DayTag, DayNote):
        assert not model.objects.exists()
    with pytest.raises(LogError):
        logbook.undo(op)


# --- expenses --------------------------------------------------------------


def test_expense_defaults_to_cad_and_resolves_category_alias():
    log(ExpenseLog(amount=12.5, category="Groceries", note="milk"))
    e = Expense.objects.get()
    assert (e.amount, e.currency, e.amount_base, e.category.slug) == (Decimal("12.50"), "CAD", Decimal("12.50"), "food")
    assert metrics.value("expenses.total", D) == Decimal("12.50")


def test_foreign_currency_uses_the_manual_rate():
    with pytest.raises(LogError, match="exchange rate"):
        log(ExpenseLog(amount=10, category="food", currency="usd"))
    ExchangeRate.objects.create(currency="usd", rate=Decimal("1.37"))
    log(ExpenseLog(amount=10, category="food", currency="usd"))
    e = Expense.objects.get()
    assert (e.amount, e.currency, e.amount_base) == (Decimal("10.00"), "USD", Decimal("13.70"))


@pytest.mark.parametrize("amount", [0, -5, "abc", "nan"])
def test_bad_amounts_are_rejected(amount):
    with pytest.raises(LogError):
        log(ExpenseLog(amount=amount, category="food"))


def test_days_without_expenses_total_zero():
    assert metrics.value("expenses.total", D) == 0


# --- sleep -----------------------------------------------------------------


def test_slept_then_woke_makes_one_session_on_the_wake_day():
    log(SleepLog(bedtime=local(2026, 11, 14, 23, 40)))
    s = SleepSession.objects.get()
    assert s.day == date(2026, 11, 15) and s.duration_min is None
    assert metrics.value("sleep.duration", date(2026, 11, 15)) is None

    op = log(SleepLog(wake_time=local(2026, 11, 15, 7, 10)))
    s = SleepSession.objects.get()
    assert s.day == date(2026, 11, 15) and s.duration_min == 450
    assert metrics.value("sleep.duration", date(2026, 11, 15)) == 450

    logbook.undo(op)  # undoing the wake reopens the session
    assert SleepSession.objects.get().wake_time is None


def test_bedtime_after_midnight_still_lands_on_the_wake_day():
    log(SleepLog(bedtime=local(2026, 11, 15, 1, 30)))
    assert SleepSession.objects.get().day == date(2026, 11, 15)
    log(SleepLog(wake_time=local(2026, 11, 15, 8, 0)))
    assert metrics.value("sleep.duration", date(2026, 11, 15)) == 390


def test_wake_without_an_open_session_is_recorded_alone():
    log(SleepLog(bedtime=local(2026, 11, 10, 23, 0)))  # stale, days ago
    log(SleepLog(wake_time=local(2026, 11, 15, 7, 0)))
    assert SleepSession.objects.count() == 2
    assert metrics.value("sleep.duration", date(2026, 11, 15)) is None
    assert "sleep.duration" not in metrics.unlogged(date(2026, 11, 15))


def test_whole_night_in_one_intent_and_validation():
    log(SleepLog(bedtime=local(2026, 11, 13, 23, 0), wake_time=local(2026, 11, 14, 6, 30)))
    assert metrics.value("sleep.duration", D) == 450
    for bad in (
        SleepLog(),
        SleepLog(bedtime=local(2026, 11, 14, 8), wake_time=local(2026, 11, 14, 7)),
        SleepLog(bedtime=local(2026, 11, 12, 8), wake_time=local(2026, 11, 14, 7)),
    ):
        with pytest.raises(LogError):
            log(bad)


def test_sleep_duration_across_dst_change_uses_real_elapsed_time():
    # Clocks go back at 02:00 on Nov 1: 23:00 -> 07:00 is nine hours.
    log(SleepLog(bedtime=local(2026, 10, 31, 23, 0), wake_time=local(2026, 11, 1, 7, 0)))
    assert metrics.value("sleep.duration", date(2026, 11, 1)) == 540


# --- workouts, notes, tags -------------------------------------------------


def test_workouts():
    log(WorkoutLog(type="Legs", duration_min=60), WorkoutLog(type="cardio"))
    assert metrics.value("exercise.sessions", D) == 2
    assert metrics.value("exercise.minutes", D) == 60
    for bad in (WorkoutLog(type="yoga"), WorkoutLog(type="push", duration_min=0)):
        with pytest.raises(LogError):
            log(bad)


def test_notes_append_or_replace_and_tags_do_not_duplicate():
    log(DayNoteLog(note="first"))
    log(DayNoteLog(note="second"))
    assert DayNote.objects.get().note == "first\nsecond"
    log(DayNoteLog(note="only", append=False))
    assert DayNote.objects.get().note == "only"

    log(DayTagLog(tags=["Sick", "travel"]))
    log(DayTagLog(tags=["sick", "deadline"]))
    assert sorted(DayTag.objects.values_list("tag__slug", flat=True)) == ["deadline", "sick", "travel"]


# --- edit and remove -------------------------------------------------------


def test_edit_revalidates_and_can_be_undone():
    log(ExpenseLog(amount=10, category="food"))
    e = Expense.objects.get()
    ExchangeRate.objects.create(currency="USD", rate=Decimal("1.40"))
    op = logbook.edit(e, source="web", amount="20", currency="usd", category="dining")
    e.refresh_from_db()
    assert (e.amount_base, e.category.slug) == (Decimal("28.00"), "eating-out")
    with pytest.raises(LogError):
        logbook.edit(e, source="web", amount=-1)
    with pytest.raises(LogError):
        logbook.edit(e, source="web", amount_base=1)  # not an editable field
    logbook.undo(op)
    e.refresh_from_db()
    assert (e.amount, e.currency, e.amount_base, e.category.slug) == (Decimal("10.00"), "CAD", Decimal("10.00"), "food")


def test_editing_the_time_moves_the_row_to_the_right_day():
    log(TrackerLog(tracker="coffee"))
    e = Entry.objects.get()
    logbook.edit(e, source="web", occurred_at=local(2026, 11, 13, 2, 0))
    e.refresh_from_db()
    assert e.day == date(2026, 11, 12)
    with pytest.raises(LogError):
        logbook.edit(e, source="web", value=-3)


def test_remove_then_undo_restores_the_identical_row():
    log(TrackerLog(tracker="coffee", note="flat white", at=local(2026, 11, 14, 9, 15)))
    before = Entry.objects.values().get()
    op = logbook.remove(Entry.objects.get(), source="telegram")
    assert coffee() is None
    logbook.undo(op)
    after = Entry.objects.values().get()
    before.pop("updated_at"), after.pop("updated_at")
    assert before == after


# --- recurring expenses ----------------------------------------------------


def rule(**kw):
    kw.setdefault("category", ExpenseCategory.objects.get(slug="housing"))
    return RecurringExpense.objects.create(**{"name": "Rent", "amount": Decimal(1650), **kw})


def test_recurring_is_idempotent_and_catches_up():
    r = rule(start_date=date(2026, 9, 1))
    assert logbook.materialize_recurring(date(2026, 8, 31)) is None
    logbook.materialize_recurring(date(2026, 11, 14))
    assert logbook.materialize_recurring(date(2026, 11, 14)) is None
    days_charged = sorted(Expense.objects.values_list("day", flat=True))
    assert days_charged == [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]
    e = Expense.objects.first()
    assert (e.source, e.recurring, e.note) == ("recurring", r, "Rent")
    r.refresh_from_db()
    assert r.next_due == date(2026, 12, 1)


def test_month_end_rule_does_not_drift():
    rule(start_date=date(2026, 1, 31))
    logbook.materialize_recurring(date(2026, 4, 30))
    assert sorted(Expense.objects.values_list("day", flat=True)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)
    ]


def test_weekly_and_yearly_rules():
    rule(name="Cleaner", start_date=date(2026, 11, 2), frequency="weekly")
    rule(name="Domain", start_date=date(2024, 2, 29), frequency="yearly")
    logbook.materialize_recurring(date(2026, 11, 16))
    assert sorted(Expense.objects.filter(note="Cleaner").values_list("day", flat=True)) == [
        date(2026, 11, 2), date(2026, 11, 9), date(2026, 11, 16)
    ]
    assert sorted(Expense.objects.filter(note="Domain").values_list("day", flat=True)) == [
        date(2024, 2, 29), date(2025, 2, 28), date(2026, 2, 28)
    ]


def test_undoing_a_recurring_run_rewinds_the_schedule():
    r = rule(start_date=date(2026, 11, 1))
    op = logbook.materialize_recurring(date(2026, 11, 14))
    logbook.undo(op)
    r.refresh_from_db()
    assert not Expense.objects.exists() and r.next_due == date(2026, 11, 1)
    logbook.materialize_recurring(date(2026, 11, 14))
    assert Expense.objects.count() == 1


def test_inactive_rules_are_skipped():
    rule(start_date=date(2026, 11, 1), is_active=False)
    assert logbook.materialize_recurring(date(2026, 11, 14)) is None


def test_sending_the_same_night_again_replaces_it():
    night = SleepLog(bedtime=local(2026, 11, 14, 23, 30), wake_time=local(2026, 11, 15, 7, 10))
    log(night)
    op = log(SleepLog(bedtime=local(2026, 11, 14, 23, 45), wake_time=local(2026, 11, 15, 7, 15)))
    assert SleepSession.objects.count() == 1
    assert metrics.value("sleep.duration", date(2026, 11, 15)) == 450
    logbook.undo(op)  # undo puts the first version back
    assert metrics.value("sleep.duration", date(2026, 11, 15)) == 460

    # A nap the same day doesn't overlap, so it is its own record.
    log(SleepLog(bedtime=local(2026, 11, 15, 14, 0), wake_time=local(2026, 11, 15, 14, 40)))
    assert SleepSession.objects.count() == 2


def test_a_whole_night_completes_an_open_bedtime():
    log(SleepLog(bedtime=local(2026, 11, 14, 23, 40)))
    log(SleepLog(bedtime=local(2026, 11, 14, 23, 30), wake_time=local(2026, 11, 15, 7, 10)))
    session = SleepSession.objects.get()
    assert session.duration_min == 460 and session.day == date(2026, 11, 15)
