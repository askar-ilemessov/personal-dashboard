"""Plain-language description of intents, for previews, toasts and bot replies."""

from tracking import days, fmt, logbook
from tracking.logbook import (
    DayNoteLog,
    DayTagLog,
    ExpenseLog,
    NoSpendLog,
    SleepLog,
    TrackerLog,
    WorkoutLog,
)
from tracking.models import AppSettings, Tracker


def describe(intent, settings: AppSettings | None = None) -> str:
    settings = settings or AppSettings.load()
    text = _describe(intent, settings)
    if intent.day and not isinstance(intent, SleepLog):
        text += f" ({intent.day:%b} {intent.day.day})"
    return text


def _describe(intent, s: AppSettings) -> str:
    if isinstance(intent, TrackerLog):
        tracker = logbook.resolve_tracker(intent.tracker)
        note = f" · {intent.note}" if intent.note else ""
        v = intent.value
        if tracker.kind == Tracker.Kind.HABIT:
            return f"{tracker.name}: {'done' if v is None or float(v) >= 1 else 'not done'}{note}"
        if tracker.kind == Tracker.Kind.DURATION:
            return f"{tracker.name}: {fmt.minutes(v)}{note}"
        if tracker.kind == Tracker.Kind.SCALE:
            return f"{tracker.name}: {fmt.number(v)}{note}"
        if intent.mode == "set":
            return f"{tracker.name} = {fmt.number(v)}{note}"
        return f"{tracker.name} +{fmt.number(1 if v is None else v)}{note}"
    if isinstance(intent, ExpenseLog):
        category = logbook.resolve_category(intent.category)
        amount = fmt.money(intent.amount, (intent.currency or s.default_currency).upper(), s.default_currency)
        return " · ".join(x for x in (amount, category.name, intent.note) if x)
    if isinstance(intent, SleepLog):
        bed = days.to_aware(intent.bedtime, s) if intent.bedtime else None
        wake = days.to_aware(intent.wake_time, s) if intent.wake_time else None
        if bed and wake:
            length = fmt.minutes((wake - bed).total_seconds() // 60)
            return f"Slept {fmt.clock(bed, s.tz)} → {fmt.clock(wake, s.tz)} ({length})"
        if bed:
            return f"Bedtime {bed.astimezone(s.tz):%a %H:%M}"
        if wake:
            return f"Woke up {wake.astimezone(s.tz):%a %H:%M}"
        return "Sleep"
    if isinstance(intent, WorkoutLog):
        parts = [intent.type.capitalize()]
        if intent.duration_min:
            parts.append(fmt.minutes(intent.duration_min))
        if intent.notes:
            parts.append(intent.notes)
        return "Workout: " + " · ".join(parts)
    if isinstance(intent, DayNoteLog):
        return f"Note: {intent.note}"
    if isinstance(intent, NoSpendLog):
        return "Nothing spent"
    if isinstance(intent, DayTagLog):
        return "Tags: " + " ".join(f"#{t}" for t in intent.tags)
    return str(intent)
