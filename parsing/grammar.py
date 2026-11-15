"""Deterministic quick-log grammar: the first tier of the shared parser.

Turns short text into logbook intents without any network call. Used by the
command palette, bot commands and (phase 4) as the fast path before the LLM.

    coffee                      2 coffees               no alcohol
    spent 12.5 food milk        $40 usd dining          18 transport uber
    gym legs 60                 cardio 35m easy run     screen 3h20
    woke 7:10                   slept 23:30             slept 23:30-7:10
    mood 4, energy 3            read                    skip meditate
    #sick #travel               note: long day          yesterday coffee 3
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from tracking import days
from tracking.logbook import (
    DayNoteLog,
    DayTagLog,
    ExpenseLog,
    SleepLog,
    TrackerLog,
    WorkoutLog,
)
from tracking.models import AppSettings, ExchangeRate, ExpenseCategory, Tracker, Workout


@dataclass
class Parsed:
    intents: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.intents) and not self.errors


class _Unparsed(Exception):
    pass


# --- Small value parsers (also used by web forms) --------------------------

_DURATION = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(?P<m>\d+)\s*(?:m(?:in(?:utes?|s)?)?)?)?$"
)


def parse_duration(text: str) -> int | None:
    """Minutes from "3h20", "3h 20m", "1.5h", "45m", "200" or "3:20"."""
    s = (text or "").strip().lower()
    if not s:
        return None
    if m := re.fullmatch(r"(\d{1,2}):(\d{2})", s):
        return int(m[1]) * 60 + int(m[2])
    m = _DURATION.match(s)
    if not m or (m["h"] is None and m["m"] is None):
        return None
    return round(float(m["h"] or 0) * 60) + int(m["m"] or 0)


def parse_clock(text: str) -> time | None:
    """Time of day from "7", "7:10", "7am", "7:10pm", "23:30"."""
    m = re.fullmatch(r"(\d{1,2})(?:[:.h](\d{2}))?\s*(am|pm)?", (text or "").strip().lower())
    if not m:
        return None
    hour, minute, half = int(m[1]), int(m[2] or 0), m[3]
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


_AMOUNT = re.compile(r"^\$?(\d+(?:[.,]\d{1,2})?)\$?([a-z]{3})?$")


def _amount(token: str):
    """("12.50", "USD" | None) or None."""
    m = _AMOUNT.match(token.lower())
    return (m[1].replace(",", "."), (m[2] or "").upper() or None) if m else None


def _count(token: str):
    m = re.fullmatch(r"[x+]?(\d+(?:\.\d+)?)x?", token.lower())
    return m[1] if m else None


# --- Parser ----------------------------------------------------------------

_WORKOUT_WORDS = {"gym", "workout", "exercise", "trained", "training"}
_EXPENSE_WORDS = {"spent", "spend", "paid", "expense", "bought"}
_WAKE_WORDS = {"woke", "wake", "awake"}
_BED_WORDS = {"slept", "sleep", "bed", "bedtime", "asleep"}
_ZERO_WORDS = {"no", "skip", "skipped", "zero", "none"}
_DONE_WORDS = {"done", "yes", "did", "✓"}
_FILLER = {"on", "for", "at", "in", "of"}


class _Parser:
    def __init__(self, now: datetime | None):
        self.settings = AppSettings.load()
        self.now = (now or timezone.now()).astimezone(self.settings.tz)
        self.day: date | None = None  # explicit backfill day
        self.trackers = self._names(Tracker.objects.filter(is_active=True))
        self.categories = self._names(ExpenseCategory.objects.filter(is_active=True))
        self.currencies = {self.settings.default_currency, *ExchangeRate.objects.values_list("currency", flat=True)}

    @staticmethod
    def _names(queryset) -> dict:
        out = {}
        for obj in queryset:
            for name in (obj.slug, obj.name.lower(), *obj.aliases):
                out.setdefault(name, obj)
                out.setdefault(name + "s", obj)
        return out

    @staticmethod
    def _match(names: dict, tokens: list[str]):
        """Longest leading run of tokens that names something -> (obj, rest)."""
        for k in (3, 2, 1):
            if len(tokens) >= k and (obj := names.get(" ".join(tokens[:k]).lower())):
                return obj, tokens[k:]
        return None, tokens

    def _when(self) -> dict:
        return {"day": self.day} if self.day else {}

    # -- whole input --

    def parse(self, text: str) -> Parsed:
        out = Parsed()
        text = " ".join((text or "").split())
        if not text:
            return out
        text = self._take_day(text)
        if m := re.match(r"^note[:\s]\s*(.+)$", text, re.IGNORECASE):  # notes keep their commas
            out.intents.append(DayNoteLog(note=m[1], **self._when()))
            return out
        for clause in re.split(r"\s*(?:,(?!\d)|;)\s*|\s+and\s+|\s+&\s+", text):
            if not clause:
                continue
            try:
                out.intents.extend(self._clause(clause.split()))
            except _Unparsed as e:
                out.errors.append(str(e) or f"Couldn't understand “{clause}”.")
        return out

    def _take_day(self, text: str) -> str:
        first, _, rest = text.partition(" ")
        today = days.logical_day(self.now, self.settings)
        word = first.lower().rstrip(":")
        if word == "yesterday":
            self.day = today - timedelta(days=1)
        elif word == "today":
            self.day = None
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", word):
            try:
                self.day = date.fromisoformat(word)
            except ValueError:
                return text
        else:
            return text
        return rest

    # -- one clause --

    def _clause(self, t: list[str]) -> list:
        head = t[0].lower().rstrip(":")
        if all(x.startswith("#") and len(x) > 1 for x in t):
            return [DayTagLog(tags=[x[1:] for x in t], **self._when())]
        if head in ("tag", "tags") and len(t) > 1:
            return [DayTagLog(tags=[x.lstrip("#") for x in t[1:]], **self._when())]
        if head == "note" and len(t) > 1:
            return [DayNoteLog(note=" ".join(t[1:]), **self._when())]
        if head in _EXPENSE_WORDS:
            return [self._expense(t[1:])]
        if head in _WORKOUT_WORDS or head in Workout.Type.values:
            return [self._workout(t)]
        if head in _WAKE_WORDS:
            return [self._wake(t[1:])]
        if head in _BED_WORDS:
            return [self._sleep(t[1:])]
        if head in _ZERO_WORDS and len(t) > 1:
            tracker, rest = self._match(self.trackers, t[1:])
            if tracker and not rest:
                return [TrackerLog(tracker=tracker.slug, value=0, mode="set", **self._when())]
            raise _Unparsed

        tracker, rest = self._match(self.trackers, t)
        if tracker:
            return [self._tracker(tracker, rest)]

        if _amount(t[0]) and len(t) > 1:
            # "2 coffees" is a count; "12.5 food" is money.
            tracker, rest = self._match(self.trackers, t[1:])
            if tracker and _count(t[0]):
                return [self._tracker(tracker, [t[0], *rest])]
            return [self._expense(t)]
        raise _Unparsed

    def _tracker(self, tracker: Tracker, rest: list[str]) -> TrackerLog:
        base = {"tracker": tracker.slug, **self._when()}
        if not rest:
            if tracker.kind in (Tracker.Kind.SCALE, Tracker.Kind.DURATION):
                raise _Unparsed(f"{tracker.name} needs a value, e.g. “{tracker.slug} {self._example(tracker)}”.")
            return TrackerLog(**base)
        word = rest[0].lower()
        note = " ".join(rest[1:])
        if word in _DONE_WORDS:
            return TrackerLog(value=1, note=note, **base)
        if word in _ZERO_WORDS:
            return TrackerLog(value=0, mode="set", note=note, **base)
        if tracker.kind == Tracker.Kind.DURATION:
            # Allow "screen 3h 20m": take as many tokens as still parse.
            for k in (2, 1):
                if (mins := parse_duration(" ".join(rest[:k]))) is not None:
                    return TrackerLog(value=mins, note=" ".join(rest[k:]), **base)
            raise _Unparsed(f"Couldn't read “{' '.join(rest)}” as a duration. Try “{tracker.slug} 3h20”.")
        if word.startswith("=") and (n := _count(word[1:])):
            return TrackerLog(value=n, mode="set", note=note, **base)
        if n := _count(word):
            mode = "set" if n == "0" else "auto"
            return TrackerLog(value=n, mode=mode, note=note, **base)
        if tracker.kind in (Tracker.Kind.HABIT, Tracker.Kind.COUNTER):
            return TrackerLog(note=" ".join(rest), **base)  # "coffee flat white"
        raise _Unparsed(f"{tracker.name} needs a number, e.g. “{tracker.slug} {self._example(tracker)}”.")

    @staticmethod
    def _example(tracker: Tracker) -> str:
        return "3h20" if tracker.kind == Tracker.Kind.DURATION else "4"

    def _expense(self, t: list[str]) -> ExpenseLog:
        if not t or not (amount := _amount(t[0])):
            raise _Unparsed("Expenses look like “spent 12.50 food lunch”.")
        value, currency = amount
        rest = t[1:]
        if rest and not currency and rest[0].upper() in self.currencies and len(rest[0]) == 3:
            currency, rest = rest[0].upper(), rest[1:]
        while rest and rest[0].lower() in _FILLER:
            rest = rest[1:]
        category, note = self._match(self.categories, rest)
        if category is None:
            known = ", ".join(c.slug for c in dict.fromkeys(self.categories.values()))
            what = f"Unknown category “{rest[0]}”" if rest else "Which category?"
            raise _Unparsed(f"{what} Known: {known}.")
        return ExpenseLog(amount=value, currency=currency, category=category.slug, note=" ".join(note), **self._when())

    def _workout(self, t: list[str]) -> WorkoutLog:
        rest = t[1:] if t[0].lower() in _WORKOUT_WORDS else t
        kind = "other"
        if rest and rest[0].lower() in Workout.Type.values:
            kind, rest = rest[0].lower(), rest[1:]
        duration = None
        for k in (2, 1):
            if len(rest) >= k and (mins := parse_duration(" ".join(rest[:k]))) is not None:
                duration, rest = mins, rest[k:]
                break
        return WorkoutLog(type=kind, duration_min=duration, notes=" ".join(rest), **self._when())

    # -- sleep --

    def _recent(self, t: time) -> datetime:
        """Most recent time the clock showed `t` (with a little slack for
        typing "woke 7:10" at 7:05)."""
        moment = datetime.combine(self.now.date(), t, tzinfo=self.settings.tz)
        if moment > self.now + timedelta(minutes=30):
            moment -= timedelta(days=1)
        return moment

    def _on_day(self, t: time, offset: int = 0) -> datetime:
        return datetime.combine(self.day + timedelta(days=offset), t, tzinfo=self.settings.tz)

    def _clock(self, tokens: list[str]) -> tuple[time | None, list[str]]:
        tokens = [x for x in tokens if x.lower() not in ("at", "up", "to", "until", "from")]
        for k in (2, 1):  # "7 am"
            if len(tokens) >= k and (c := parse_clock("".join(tokens[:k]))):
                return c, tokens[k:]
        return None, tokens

    def _wake(self, rest: list[str]) -> SleepLog:
        clock, extra = self._clock(rest)
        if extra:
            raise _Unparsed(f"Couldn't read “{' '.join(rest)}” as a time. Try “woke 7:10”.")
        if clock is None:
            if self.day:
                raise _Unparsed("Add a time when backfilling, e.g. “yesterday woke 7:10”.")
            return SleepLog(wake_time=self.now)
        return SleepLog(wake_time=self._on_day(clock) if self.day else self._recent(clock))

    def _sleep(self, rest: list[str]) -> SleepLog:
        joined = " ".join(rest).lower()
        parts = re.split(r"\s*(?:-|–|→|\bto\b|\buntil\b|\bwoke(?: up)?(?: at)?\b)\s*", joined, maxsplit=1)
        bed, extra = self._clock(parts[0].split())
        if extra or (rest and bed is None):
            raise _Unparsed(f"Couldn't read “{' '.join(rest)}”. Try “slept 23:30” or “slept 23:30-7:10”.")
        if len(parts) == 2:
            wake, extra = self._clock(parts[1].split())
            if wake is None or extra:
                raise _Unparsed(f"Couldn't read “{parts[1]}” as a wake time.")
            wake_at = self._on_day(wake) if self.day else self._recent(wake)
            bed_at = datetime.combine(wake_at.date(), bed, tzinfo=self.settings.tz)
            if bed_at >= wake_at:
                bed_at -= timedelta(days=1)
            return SleepLog(bedtime=bed_at, wake_time=wake_at)
        if bed is None:
            if self.day:
                raise _Unparsed("Add a time when backfilling, e.g. “yesterday slept 23:30”.")
            return SleepLog(bedtime=self.now)
        if self.day:  # evening times are the night before the wake day
            return SleepLog(bedtime=self._on_day(bed, -1 if bed.hour >= 15 else 0), day=self.day)
        return SleepLog(bedtime=self._recent(bed))


def parse(text: str, now: datetime | None = None) -> Parsed:
    return _Parser(now).parse(text)
