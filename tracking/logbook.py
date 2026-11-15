"""The shared logging service.

Every writer (web, Telegram bot, Shortcuts endpoint, parser, seed, recurring
rules) builds a list of intents and calls `apply`. Nothing else creates,
edits or deletes logged rows, so day assignment, validation and undo behave
identically everywhere.

    op = apply([TrackerLog(tracker="coffee")], source="telegram", raw_text="/coffee")
    undo(op)
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from . import days
from .models import (
    AppSettings,
    DayNote,
    DayTag,
    Entry,
    ExchangeRate,
    Expense,
    ExpenseCategory,
    Operation,
    RecurringExpense,
    SleepSession,
    Tag,
    Tracker,
    Workout,
)


class LogError(ValueError):
    """Invalid intent. The message is safe to show to the user."""


# --- Intents ---------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Intent:
    at: datetime | None = None  # when it happened; defaults to now
    day: date | None = None  # explicit logical day (backfill); overrides the one derived from `at`


@dataclass(frozen=True, kw_only=True)
class TrackerLog(Intent):
    """Habit, counter, duration or scale. `value` defaults to 1 for habits
    and counters. mode: "add" appends an entry, "set" replaces the day's
    value, "auto" picks add for sum trackers and set for the rest."""

    tracker: str
    value: Decimal | int | float | str | None = None
    mode: str = "auto"
    note: str = ""


@dataclass(frozen=True, kw_only=True)
class ExpenseLog(Intent):
    amount: Decimal | int | float | str
    category: str
    currency: str | None = None
    note: str = ""


@dataclass(frozen=True, kw_only=True)
class SleepLog(Intent):
    """Bedtime only opens a session; wake time only closes the open one;
    both together record a whole night."""

    bedtime: datetime | None = None
    wake_time: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class WorkoutLog(Intent):
    type: str
    duration_min: int | None = None
    notes: str = ""


@dataclass(frozen=True, kw_only=True)
class DayNoteLog(Intent):
    note: str
    append: bool = True


@dataclass(frozen=True, kw_only=True)
class DayTagLog(Intent):
    tags: tuple[str, ...] | list[str]


# --- Lookup ----------------------------------------------------------------


def _resolve(model, text: str, label: str):
    key = (text or "").strip().lower()
    if not key:
        raise LogError(f"Missing {label}.")
    for obj in model.objects.filter(is_active=True):
        if key in (obj.slug, obj.name.lower(), *obj.aliases):
            return obj
    known = ", ".join(model.objects.filter(is_active=True).values_list("slug", flat=True))
    raise LogError(f"Unknown {label} “{text}”. Known: {known}.")


def resolve_tracker(text: str) -> Tracker:
    return _resolve(Tracker, text, "tracker")


def resolve_category(text: str) -> ExpenseCategory:
    return _resolve(ExpenseCategory, text, "category")


# --- Change recording ------------------------------------------------------


def _snapshot(obj) -> dict:
    """Field values as JSON-safe data. Datetimes are written out in full
    because the JSON encoder would truncate them to milliseconds."""
    out = {}
    for f in obj._meta.concrete_fields:
        value = f.value_from_object(obj)
        out[f.attname] = value.isoformat() if isinstance(value, datetime) else value
    return out


@dataclass
class _Ctx:
    settings: AppSettings
    operation: Operation
    now: datetime
    changes: list = field(default_factory=list)

    def _record(self, obj, action, before=None):
        self.changes.append(
            {"model": obj._meta.label_lower, "pk": obj.pk, "action": action, "before": before}
        )

    def create(self, model, **fields):
        obj = model.objects.create(operation=self.operation, source=self.operation.source, **fields)
        self._record(obj, "create")
        return obj

    def update(self, obj, **fields):
        before = _snapshot(obj)
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
        self._record(obj, "update", before)
        return obj

    def delete(self, obj):
        before, pk = _snapshot(obj), obj.pk
        obj.delete()
        obj.pk = pk
        self._record(obj, "delete", before)

    def when(self, intent: Intent) -> tuple[datetime, date]:
        """(timestamp, logical day) for an intent."""
        if intent.at is not None:
            at = days.to_aware(intent.at, self.settings)
            return at, intent.day or days.logical_day(at, self.settings)
        if intent.day is not None:
            return days.local_noon(intent.day, self.settings), intent.day
        return self.now, days.logical_day(self.now, self.settings)

    def timed(self, intent: Intent) -> dict:
        at, day = self.when(intent)
        return {"occurred_at": at, "day": day, "utc_offset_min": days.utc_offset_min(at, self.settings)}


# --- Validation ------------------------------------------------------------


def _decimal(value, label="value") -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise LogError(f"“{value}” is not a number.") from None
    if not d.is_finite():
        raise LogError(f"“{value}” is not a number.")
    if d.as_tuple().exponent < -2:
        d = d.quantize(Decimal("0.01"))
    if abs(d) >= Decimal(100000000):
        raise LogError(f"{label.capitalize()} is too large.")
    return d


def _tracker_value(tracker: Tracker, value) -> Decimal:
    if value is None:
        if tracker.kind in (Tracker.Kind.HABIT, Tracker.Kind.COUNTER):
            return Decimal(1)
        raise LogError(f"{tracker.name} needs a value.")
    if isinstance(value, bool):
        value = int(value)
    d = _decimal(value)
    if d < 0:
        raise LogError(f"{tracker.name} can't be negative.")
    if tracker.kind == Tracker.Kind.HABIT and d not in (0, 1):
        raise LogError(f"{tracker.name} is a habit: use 1 (done) or 0 (not done).")
    if tracker.min_value is not None and d < tracker.min_value:
        raise LogError(f"{tracker.name} must be at least {tracker.min_value.normalize():f}.")
    if tracker.max_value is not None and d > tracker.max_value:
        raise LogError(f"{tracker.name} must be at most {tracker.max_value.normalize():f}.")
    return d


def _base_amount(amount: Decimal, currency: str, settings: AppSettings) -> Decimal:
    if currency == settings.default_currency:
        return amount
    rate = ExchangeRate.objects.filter(currency=currency).first()
    if rate is None:
        raise LogError(f"No exchange rate for {currency}. Add one in Settings first.")
    return (amount * rate.rate).quantize(Decimal("0.01"))


# --- Intent handlers -------------------------------------------------------


def _apply_tracker(intent: TrackerLog, ctx: _Ctx):
    tracker = resolve_tracker(intent.tracker)
    value = _tracker_value(tracker, intent.value)
    if intent.mode not in ("auto", "add", "set"):
        raise LogError(f"Unknown mode “{intent.mode}”.")
    is_sum = tracker.aggregation == Tracker.Aggregation.SUM
    if intent.mode == "add" and not is_sum:
        raise LogError(f"{tracker.name} holds one value per day; it can't be added to.")
    replace = intent.mode == "set" or not is_sum

    timed = ctx.timed(intent)
    if replace:
        existing = list(Entry.objects.filter(tracker=tracker, day=timed["day"]).order_by("id"))
        if len(existing) == 1:
            # Keep the row (and its original timestamp unless one was given).
            fields = {"value": value}
            if intent.note:
                fields["note"] = intent.note
            if intent.at is not None:
                fields |= {"occurred_at": timed["occurred_at"], "utc_offset_min": timed["utc_offset_min"]}
            ctx.update(existing[0], **fields)
            return
        for row in existing:
            ctx.delete(row)
    ctx.create(Entry, tracker=tracker, value=value, note=intent.note, **timed)


def _apply_expense(intent: ExpenseLog, ctx: _Ctx):
    amount = _decimal(intent.amount, "amount")
    if amount <= 0:
        raise LogError("Amount must be more than zero.")
    currency = (intent.currency or ctx.settings.default_currency).upper()
    ctx.create(
        Expense,
        amount=amount,
        currency=currency,
        amount_base=_base_amount(amount, currency, ctx.settings),
        category=resolve_category(intent.category),
        note=intent.note,
        **ctx.timed(intent),
    )


OPEN_SESSION_WINDOW = timedelta(hours=20)


def _apply_sleep(intent: SleepLog, ctx: _Ctx):
    s = ctx.settings
    bed = days.to_aware(intent.bedtime, s) if intent.bedtime else None
    wake = days.to_aware(intent.wake_time, s) if intent.wake_time else None
    if not bed and not wake:
        raise LogError("Sleep needs a bedtime, a wake time, or both.")

    def wake_day(moment):
        return intent.day or moment.astimezone(s.tz).date()

    if bed and wake:
        if not timedelta(0) < wake - bed <= timedelta(hours=24):
            raise LogError("Wake time must be after bedtime, within 24 hours.")
        ctx.create(SleepSession, bedtime=bed, wake_time=wake, day=wake_day(wake))
    elif bed:
        provisional = intent.day or days.logical_day(bed, s) + timedelta(days=1)
        ctx.create(SleepSession, bedtime=bed, day=provisional)
    else:
        open_session = (
            SleepSession.objects.filter(
                wake_time__isnull=True, bedtime__lt=wake, bedtime__gte=wake - OPEN_SESSION_WINDOW
            )
            .order_by("-bedtime")
            .first()
        )
        if open_session:
            ctx.update(open_session, wake_time=wake, day=wake_day(wake))
        else:
            ctx.create(SleepSession, wake_time=wake, day=wake_day(wake))


def _apply_workout(intent: WorkoutLog, ctx: _Ctx):
    kind = (intent.type or "").strip().lower()
    if kind not in Workout.Type.values:
        raise LogError(f"Unknown workout type “{intent.type}”. Use: {', '.join(Workout.Type.values)}.")
    duration = intent.duration_min
    if duration is not None:
        duration = int(_decimal(duration, "duration"))
        if not 0 < duration <= 24 * 60:
            raise LogError("Workout duration must be between 1 minute and 24 hours.")
    ctx.create(Workout, type=kind, duration_min=duration, notes=intent.notes, **ctx.timed(intent))


def _apply_note(intent: DayNoteLog, ctx: _Ctx):
    text = intent.note.strip()
    if not text:
        raise LogError("The note is empty.")
    _, day = ctx.when(intent)
    existing = DayNote.objects.filter(day=day).first()
    if existing:
        ctx.update(existing, note=f"{existing.note}\n{text}" if intent.append else text)
    else:
        ctx.create(DayNote, day=day, note=text)


def _apply_tags(intent: DayTagLog, ctx: _Ctx):
    _, day = ctx.when(intent)
    slugs = [slugify(t) for t in intent.tags]
    if not slugs or not all(slugs):
        raise LogError("Tags can't be empty.")
    for slug in dict.fromkeys(slugs):
        tag, _ = Tag.objects.get_or_create(slug=slug)
        if not DayTag.objects.filter(day=day, tag=tag).exists():
            ctx.create(DayTag, day=day, tag=tag)


_HANDLERS = {
    TrackerLog: _apply_tracker,
    ExpenseLog: _apply_expense,
    SleepLog: _apply_sleep,
    WorkoutLog: _apply_workout,
    DayNoteLog: _apply_note,
    DayTagLog: _apply_tags,
}


# --- Public API ------------------------------------------------------------


def _begin(source: str, raw_text: str, now: datetime | None) -> _Ctx:
    now = now or timezone.now()
    op = Operation.objects.create(source=source, raw_text=raw_text, created_at=now)
    return _Ctx(settings=AppSettings.load(), operation=op, now=now)


def _finish(ctx: _Ctx) -> Operation:
    ctx.operation.changes = ctx.changes
    ctx.operation.save(update_fields=["changes"])
    ctx.operation.refresh_from_db()  # normalise snapshots to their JSON form
    return ctx.operation


@transaction.atomic
def apply(intents, source: str, raw_text: str = "", now: datetime | None = None) -> Operation:
    """Apply intents atomically: all are saved, or LogError is raised and
    none are. Returns the Operation, which `undo` can reverse."""
    intents = list(intents)
    if not intents:
        raise LogError("Nothing to log.")
    ctx = _begin(source, raw_text, now)
    for intent in intents:
        handler = _HANDLERS.get(type(intent))
        if handler is None:
            raise LogError(f"Unsupported intent {type(intent).__name__}.")
        handler(intent, ctx)
    return _finish(ctx)


_EDITABLE = {
    Entry: {"value", "note", "day", "occurred_at"},
    Expense: {"amount", "currency", "category", "note", "day", "occurred_at"},
    SleepSession: {"bedtime", "wake_time", "day"},
    Workout: {"type", "duration_min", "notes", "day", "occurred_at"},
    DayNote: {"note"},
}


@transaction.atomic
def edit(row, source: str, **fields) -> Operation:
    """Inline edit of an existing row, with the same validation as logging."""
    allowed = _EDITABLE.get(type(row))
    if allowed is None or not fields or set(fields) - allowed:
        raise LogError("Those fields can't be edited.")
    ctx = _begin(source, "", None)
    s = ctx.settings
    for name in ("occurred_at", "bedtime", "wake_time"):
        if fields.get(name):
            fields[name] = days.to_aware(fields[name], s)

    if isinstance(row, Entry) and "value" in fields:
        fields["value"] = _tracker_value(row.tracker, fields["value"])
    if isinstance(row, Expense):
        if isinstance(fields.get("category"), str):
            fields["category"] = resolve_category(fields["category"])
        amount = _decimal(fields.get("amount", row.amount), "amount")
        if amount <= 0:
            raise LogError("Amount must be more than zero.")
        currency = fields.get("currency", row.currency).upper()
        if "amount" in fields or "currency" in fields:
            fields |= {
                "amount": amount,
                "currency": currency,
                "amount_base": _base_amount(amount, currency, s),
            }
    if isinstance(row, SleepSession):
        bed = fields.get("bedtime", row.bedtime)
        wake = fields.get("wake_time", row.wake_time)
        if bed and wake and not timedelta(0) < wake - bed <= timedelta(hours=24):
            raise LogError("Wake time must be after bedtime, within 24 hours.")
        if not bed and not wake:
            raise LogError("Sleep needs a bedtime, a wake time, or both.")
    if isinstance(row, Workout):
        if "type" in fields and fields["type"] not in Workout.Type.values:
            raise LogError(f"Unknown workout type “{fields['type']}”.")
        d = fields.get("duration_min")
        if d is not None and not 0 < int(d) <= 24 * 60:
            raise LogError("Workout duration must be between 1 minute and 24 hours.")
    if isinstance(row, DayNote) and not fields["note"].strip():
        raise LogError("The note is empty. Delete it instead.")
    if "occurred_at" in fields and "day" not in fields:
        fields["day"] = days.logical_day(fields["occurred_at"], s)
        fields["utc_offset_min"] = days.utc_offset_min(fields["occurred_at"], s)

    ctx.update(row, **fields)
    return _finish(ctx)


@transaction.atomic
def remove(row, source: str) -> Operation:
    """Delete a logged row. Undoable."""
    if type(row) not in (*_EDITABLE, DayTag):
        raise LogError("That can't be deleted here.")
    ctx = _begin(source, "", None)
    ctx.delete(row)
    return _finish(ctx)


@transaction.atomic
def undo(operation: Operation) -> Operation:
    """Reverse an operation: created rows are deleted, edited and deleted
    rows are restored to their previous state."""
    operation = Operation.objects.select_for_update().get(pk=operation.pk)
    if operation.undone_at:
        raise LogError("Already undone.")
    for change in reversed(operation.changes):
        model = apps.get_model(change["model"])
        if change["action"] == "create":
            model.objects.filter(pk=change["pk"]).delete()
        else:
            values = {
                f.attname: f.to_python(change["before"][f.attname])
                for f in model._meta.concrete_fields
                if f.attname in change["before"]
            }
            model(**values).save()
    operation.undone_at = timezone.now()
    operation.save(update_fields=["undone_at"])
    return operation


# --- Recurring expenses ----------------------------------------------------


def _next_occurrence(rule: RecurringExpense, after: date) -> date:
    """First scheduled date strictly after `after`, anchored on start_date
    so that a rule starting on the 31st does not drift to the 28th."""
    start = rule.start_date
    if rule.frequency == RecurringExpense.Frequency.WEEKLY:
        weeks = max(0, (after - start).days // 7 + 1)
        return start + timedelta(weeks=weeks)
    step = 12 if rule.frequency == RecurringExpense.Frequency.YEARLY else 1
    k = 0
    while True:
        months = start.month - 1 + k * step
        year, month = start.year + months // 12, months % 12 + 1
        candidate = date(year, month, min(start.day, calendar.monthrange(year, month)[1]))
        if candidate > after:
            return candidate
        k += 1


@transaction.atomic
def materialize_recurring(today: date | None = None) -> Operation | None:
    """Create the expenses that recurring rules owe up to `today`. Safe to
    run any number of times."""
    settings = AppSettings.load()
    today = today or days.today(settings)
    due = RecurringExpense.objects.select_for_update().filter(is_active=True, next_due__lte=today)
    if not due.exists():
        return None
    ctx = _begin("recurring", "", None)
    for rule in due:
        next_due = rule.next_due
        while next_due <= today:
            day = next_due
            if not Expense.objects.filter(recurring=rule, day=day).exists():
                at = days.local_noon(day, settings)
                currency = rule.currency.upper()
                ctx.create(
                    Expense,
                    amount=rule.amount,
                    currency=currency,
                    amount_base=_base_amount(rule.amount, currency, settings),
                    category=rule.category,
                    note=rule.name,
                    recurring=rule,
                    day=day,
                    occurred_at=at,
                    utc_offset_min=days.utc_offset_min(at, settings),
                )
            next_due = _next_occurrence(rule, day)
        ctx.update(rule, next_due=next_due)  # recorded, so undo rewinds the schedule too
    return _finish(ctx)
