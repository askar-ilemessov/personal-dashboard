"""Entries tables: one definition per row type drives the display columns,
the inline edit form, filtering, sorting and CSV export."""

from dataclasses import dataclass
from datetime import date, datetime

from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html

from parsing.grammar import parse_duration
from tracking import fmt
from tracking.logbook import LogError
from tracking.models import DayNote, Entry, Expense, ExpenseCategory, SleepSession, Tracker, Workout


@dataclass
class Column:
    label: str
    sort: str | None = None
    align: str = "left"


@dataclass
class Field:
    name: str
    label: str
    type: str = "text"  # text, number, date, datetime, select, textarea
    value: str = ""
    choices: list | None = None
    hint: str = ""


def day_link(day: date):
    return format_html('<a href="{}">{}</a>', reverse("web:day", args=[day.isoformat()]), f"{day:%a, %b} {day.day}")


def _local(dt, s, pattern="%H:%M"):
    return dt.astimezone(s.tz).strftime(pattern) if dt else "—"


def _date(text):
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        raise LogError("That date isn't valid.") from None


def _datetime(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise LogError("That time isn't valid.") from None


class Table:
    key = ""
    model = None
    columns: list[Column] = []
    search = "note"
    default_sort = "-day"
    tiebreak = ("-id",)

    def queryset(self):
        return self.model.objects.all()

    def filter(self, qs, params):
        if q := params.get("q", "").strip():
            qs = qs.filter(Q(**{f"{self.search}__icontains": q}))
        return qs

    def order(self, qs, params):
        allowed = {c.sort for c in self.columns if c.sort}
        sort = params.get("sort")
        if sort in allowed:
            prefix = "" if params.get("dir") == "asc" else "-"
            return qs.order_by(prefix + sort, *self.tiebreak)
        return qs.order_by(self.default_sort, *self.tiebreak)

    def cells(self, row, s) -> list:
        raise NotImplementedError

    def fields(self, row, s) -> list[Field]:
        raise NotImplementedError

    def clean(self, row, post, s) -> dict:
        raise NotImplementedError

    csv_header: list[str] = []

    def csv_row(self, row, s) -> list:
        raise NotImplementedError


class EntryTable(Table):
    key = "entry"
    model = Entry
    columns = [
        Column("Day", "day"), Column("Time", "occurred_at"), Column("Value", "value", "right"),
        Column("Note"), Column("Source", "source"),
    ]
    with_tracker = False
    csv_header = ["day", "time", "tracker", "value", "unit", "note", "source"]

    def queryset(self):
        return Entry.objects.select_related("tracker")

    def filter(self, qs, params):
        if slug := params.get("tracker"):
            qs = qs.filter(tracker__slug=slug)
        return super().filter(qs, params)

    @staticmethod
    def _value(row):
        kind = row.tracker.kind
        if kind == Tracker.Kind.HABIT:
            return fmt.value("habit", row.value)
        if kind == Tracker.Kind.DURATION:
            return fmt.minutes(row.value)
        unit = f" {row.tracker.unit}" if row.tracker.unit and kind == Tracker.Kind.COUNTER else ""
        return fmt.number(row.value) + unit

    def cells(self, row, s):
        out = [day_link(row.day), _local(row.occurred_at, s)]
        if self.with_tracker:
            out.append(row.tracker.name)
        return [*out, self._value(row), row.note, row.get_source_display()]

    def fields(self, row, s):
        kind = row.tracker.kind
        if kind == Tracker.Kind.HABIT:
            value = Field("value", "Value", "select", str(int(row.value >= 1)), [("1", "Done"), ("0", "Not done")])
        elif kind == Tracker.Kind.DURATION:
            value = Field("value", "Value", "text", fmt.minutes(row.value), hint="e.g. 3h20")
        else:
            value = Field("value", "Value", "number", fmt.number(row.value).replace(",", ""))
        return [Field("day", "Day", "date", row.day.isoformat()), value, Field("note", "Note", "text", row.note)]

    def clean(self, row, post, s):
        value = post.get("value", "")
        if row.tracker.kind == Tracker.Kind.DURATION:
            value = parse_duration(value)
            if value is None:
                raise LogError("Couldn't read that duration. Try 3h20 or 200.")
        return {"day": _date(post.get("day")), "value": value, "note": post.get("note", "").strip()}

    def csv_row(self, row, s):
        return [row.day, _local(row.occurred_at, s), row.tracker.slug, row.value, row.tracker.unit, row.note, row.source]


class DayEntryTable(EntryTable):
    """Entries of every tracker on one day, so the tracker gets a column."""

    key = "dayentry"
    with_tracker = True
    columns = [
        Column("Day"), Column("Time"), Column("Tracker"), Column("Value", None, "right"),
        Column("Note"), Column("Source"),
    ]
    default_sort = "occurred_at"
    tiebreak = ("id",)


class ExpenseTable(Table):
    key = "expense"
    model = Expense
    columns = [
        Column("Day", "day"), Column("Amount", "amount_base", "right"), Column("Category", "category__name"),
        Column("Note", "note"), Column("Source", "source"),
    ]
    csv_header = ["day", "amount", "currency", "amount_cad", "category", "note", "recurring", "source"]

    def queryset(self):
        return Expense.objects.select_related("category", "recurring")

    def filter(self, qs, params):
        if slug := params.get("category"):
            qs = qs.filter(category__slug=slug)
        return super().filter(qs, params)

    def cells(self, row, s):
        amount = fmt.money(row.amount, row.currency, s.default_currency)
        if row.currency != s.default_currency:
            amount += f" (= {fmt.money(row.amount_base)})"
        return [day_link(row.day), amount, row.category.name, row.note, row.get_source_display()]

    def fields(self, row, s):
        categories = [(c.slug, c.name) for c in ExpenseCategory.objects.filter(is_active=True)]
        if row.category.slug not in dict(categories):
            categories.append((row.category.slug, row.category.name))
        return [
            Field("day", "Day", "date", row.day.isoformat()),
            Field("amount", "Amount", "number", str(row.amount)),
            Field("currency", "Currency", "text", row.currency),
            Field("category", "Category", "select", row.category.slug, categories),
            Field("note", "Note", "text", row.note),
        ]

    def clean(self, row, post, s):
        category = ExpenseCategory.objects.filter(slug=post.get("category")).first()
        if category is None:
            raise LogError("Pick a category.")
        return {
            "day": _date(post.get("day")),
            "amount": post.get("amount", ""),
            "currency": (post.get("currency") or s.default_currency).strip().upper(),
            "category": category,
            "note": post.get("note", "").strip(),
        }

    def csv_row(self, row, s):
        return [row.day, row.amount, row.currency, row.amount_base, row.category.slug, row.note,
                row.recurring.name if row.recurring else "", row.source]


class SleepTable(Table):
    key = "sleep"
    model = SleepSession
    columns = [
        Column("Wake day", "day"), Column("Bedtime", "bedtime"), Column("Woke up", "wake_time"),
        Column("Duration", None, "right"), Column("Source", "source"),
    ]
    search = "source"
    csv_header = ["wake_day", "bedtime", "wake_time", "duration_min", "source"]

    def cells(self, row, s):
        length = fmt.minutes(row.duration_min) if row.duration_min is not None else "—"
        return [day_link(row.day), _local(row.bedtime, s, "%a %H:%M"), _local(row.wake_time, s, "%a %H:%M"),
                length, row.get_source_display()]

    def fields(self, row, s):
        def iso(dt):
            return dt.astimezone(s.tz).strftime("%Y-%m-%dT%H:%M") if dt else ""

        return [Field("bedtime", "Bedtime", "datetime", iso(row.bedtime)),
                Field("wake_time", "Woke up", "datetime", iso(row.wake_time))]

    def clean(self, row, post, s):
        return {"bedtime": _datetime(post.get("bedtime")), "wake_time": _datetime(post.get("wake_time"))}

    def csv_row(self, row, s):
        return [row.day, _local(row.bedtime, s, "%Y-%m-%d %H:%M"), _local(row.wake_time, s, "%Y-%m-%d %H:%M"),
                row.duration_min if row.duration_min is not None else "", row.source]


class WorkoutTable(Table):
    key = "workout"
    model = Workout
    columns = [
        Column("Day", "day"), Column("Type", "type"), Column("Duration", "duration_min", "right"),
        Column("Notes", "notes"), Column("Source", "source"),
    ]
    search = "notes"
    csv_header = ["day", "time", "type", "duration_min", "notes", "source"]

    def filter(self, qs, params):
        if kind := params.get("type"):
            qs = qs.filter(type=kind)
        return super().filter(qs, params)

    def cells(self, row, s):
        length = fmt.minutes(row.duration_min) if row.duration_min else "—"
        return [day_link(row.day), row.get_type_display(), length, row.notes, row.get_source_display()]

    def fields(self, row, s):
        return [
            Field("day", "Day", "date", row.day.isoformat()),
            Field("type", "Type", "select", row.type, list(Workout.Type.choices)),
            Field("duration", "Duration", "text", fmt.minutes(row.duration_min) if row.duration_min else "",
                  hint="e.g. 60 or 1h15"),
            Field("notes", "Notes", "text", row.notes),
        ]

    def clean(self, row, post, s):
        text = post.get("duration", "").strip()
        duration = parse_duration(text) if text else None
        if text and duration is None:
            raise LogError("Couldn't read that duration. Try 60 or 1h15.")
        return {"day": _date(post.get("day")), "type": post.get("type", ""), "duration_min": duration,
                "notes": post.get("notes", "").strip()}

    def csv_row(self, row, s):
        return [row.day, _local(row.occurred_at, s), row.type, row.duration_min or "", row.notes, row.source]


class NoteTable(Table):
    key = "note"
    model = DayNote
    columns = [Column("Day"), Column("Note")]
    csv_header = ["day", "note"]

    def cells(self, row, s):
        return [day_link(row.day), row.note]

    def fields(self, row, s):
        return [Field("note", "Note", "textarea", row.note)]

    def clean(self, row, post, s):
        return {"note": post.get("note", "").strip()}

    def csv_row(self, row, s):
        return [row.day, row.note]


TABLES = {t.key: t for t in (EntryTable(), DayEntryTable(), ExpenseTable(), SleepTable(), WorkoutTable(), NoteTable())}
