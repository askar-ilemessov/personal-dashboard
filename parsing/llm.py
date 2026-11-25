"""Second parser tier: Claude turns free-form text into structured items.

The model only classifies and extracts. It never works out dates or
timestamps (it reports "day_offset" and clock times; Python resolves them
with the same rules as the grammar) and it can only name trackers and
categories that exist, because the JSON schema is built from the database.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import anthropic
from django.conf import settings as django_settings

from tracking import days
from tracking.logbook import DayNoteLog, DayTagLog, ExpenseLog, TrackerLog, WorkoutLog
from tracking.models import AppSettings, ExchangeRate, ExpenseCategory, Tracker, Workout

from .grammar import chain_sleep, parse_clock, sleep_intent

NONE = "none"
KINDS = ["tracker", "expense", "sleep", "workout", "note", "tags"]


class LlmUnavailable(Exception):
    """The model could not be reached or did not return a usable answer."""


def _nullable(kind: str, description: str) -> dict:
    return {"type": [kind, "null"], "description": description}


def schema(trackers, categories) -> dict:
    item = {
        "kind": {"type": "string", "enum": KINDS},
        "day_offset": _nullable("integer", "Days from today: 0 today, -1 yesterday. null if no day was mentioned."),
        "date": _nullable("string", "YYYY-MM-DD, only if the user wrote an explicit calendar date."),
        "time": _nullable("string", "HH:MM (24h) the event happened, if stated. Not for sleep."),
        "tracker": {"type": "string", "enum": [*(t.slug for t in trackers), NONE]},
        "value": _nullable("number", "Tracker value. Minutes for duration trackers; 1 or 0 for habits."),
        "replace": {"type": "boolean", "description": "true if the value is the day's total, false if it adds to it."},
        "amount": _nullable("number", "Expense amount, as written."),
        "currency": _nullable("string", "ISO code, only if the user named a currency."),
        "category": {"type": "string", "enum": [*(c.slug for c in categories), NONE]},
        "bedtime": _nullable("string", 'HH:MM (24h), or "now".'),
        "wake_time": _nullable("string", 'HH:MM (24h), or "now".'),
        "workout_type": {"type": "string", "enum": [*Workout.Type.values, NONE]},
        "duration_min": _nullable("integer", "Workout length in minutes."),
        "text": _nullable("string", "Note text, or a short note for an expense, workout or tracker entry."),
        "tags": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": item, "required": list(item),
                          "additionalProperties": False},
            },
            "unparsed": {"type": "string", "description": "Parts of the text you could not log. Empty if none."},
        },
        "required": ["items", "unparsed"],
        "additionalProperties": False,
    }


def system_prompt(trackers, categories, settings: AppSettings) -> str:
    def tracker_line(t: Tracker) -> str:
        bits = [f"kind={t.kind}"]
        if t.unit:
            bits.append(f"unit={t.unit}")
        if t.min_value is not None and t.max_value is not None:
            bits.append(f"range={t.min_value:f}-{t.max_value:f}".replace(".00", ""))
        if t.aliases:
            bits.append("also called: " + ", ".join(t.aliases))
        return f"- {t.slug}: {t.name} ({'; '.join(bits)})"

    def category_line(c: ExpenseCategory) -> str:
        also = f" (also: {', '.join(c.aliases)})" if c.aliases else ""
        return f"- {c.slug}: {c.name}{also}"

    currencies = sorted({settings.default_currency, *ExchangeRate.objects.values_list("currency", flat=True)})
    return f"""You turn one person's short messages about their day into structured log items for their personal tracking app. The app shows them a preview of your items and they confirm before anything is saved, so be literal: log what they said happened, and nothing else.

## What can be logged

Trackers (use the slug):
{chr(10).join(tracker_line(t) for t in trackers)}

Tracker kinds: a habit is done (value 1) or not done (value 0). A counter adds up over the day; value is how many, default 1. A duration is one total per day, in minutes. A scale is one rating per day within its range.

Expense categories (use the slug):
{chr(10).join(category_line(c) for c in categories)}

Currencies in use: {", ".join(currencies)}. The default is {settings.default_currency}; leave currency null unless the user names one.

Workout types: {", ".join(Workout.Type.values)}. Use "other" for anything that is not clearly one of the first four.

Other kinds: sleep (bedtime and/or wake_time), note (free text about the day, in `text`), tags (short labels for the day such as sick, travel, deadline).

## Rules

- One item per thing that happened. "two coffees and a beer" is two items.
- Set only the fields that belong to an item's kind. Use "{NONE}" for the enum fields that do not apply, null for the other fields that do not apply, and [] for tags.
- `replace` is true when the user states the day's total or says there was none ("no alcohol today", "3 coffees in total"). It is false when they report something to add ("had another coffee").
- Do not calculate dates or timestamps. Report the day as `day_offset` using the list of recent days in the message, or as `date` if they wrote a calendar date. Report clock times as written, converted to 24h HH:MM. For sleep, give bedtime and wake_time as clock times; use "now" for "going to bed" or "just woke up". If they woke up in the night and slept again, give one sleep item per stretch, in order.
- For a scale tracker, log a value only if the user gives a number or an unmistakable rating. On a 1-5 scale: terrible 1, bad 2, okay 3, good 4, great 5.
- If a spend has no obvious category, use "other" if that category exists; otherwise leave the spend out and mention it in `unparsed`.
- Never invent a value, amount, time or category. If part of the message is unclear, is a question, or is not something to log, leave it out of `items` and quote that part in `unparsed`.
- If nothing can be logged, return an empty `items` list and put the whole message in `unparsed`."""


def _days_table(today: date) -> str:
    rows = [f"{(today + timedelta(days=-i)):%a %Y-%m-%d} = {-i}" for i in range(8)]
    return ", ".join(rows)


def call(text: str, now: datetime, settings: AppSettings, client=None) -> dict:
    """Ask the model. Returns {"items": [...], "unparsed": "..."}."""
    trackers = list(Tracker.objects.filter(is_active=True))
    categories = list(ExpenseCategory.objects.filter(is_active=True))
    client = client or anthropic.Anthropic(api_key=django_settings.ANTHROPIC_API_KEY)
    local = now.astimezone(settings.tz)
    today = days.logical_day(now, settings)
    try:
        response = client.with_options(timeout=30.0, max_retries=1).messages.create(
            model=django_settings.PARSER_MODEL,
            max_tokens=4000,
            system=system_prompt(trackers, categories, settings),
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema(trackers, categories)}},
            messages=[{
                "role": "user",
                "content": f"Current local time: {local:%a %Y-%m-%d %H:%M}\n"
                           f"Recent days and their day_offset: {_days_table(today)}\n\n"
                           f"Message:\n{text}",
            }],
        )
    except anthropic.AuthenticationError:
        raise LlmUnavailable("the Anthropic API key was rejected") from None
    except anthropic.RateLimitError:
        raise LlmUnavailable("rate limited, try again in a moment") from None
    except anthropic.APIStatusError as e:
        raise LlmUnavailable(f"the API returned an error ({e.status_code})") from None
    except anthropic.APIConnectionError:
        raise LlmUnavailable("could not reach the API") from None

    if response.stop_reason == "refusal":
        raise LlmUnavailable("the model declined to read that message")
    if response.stop_reason == "max_tokens":
        raise LlmUnavailable("the message was too long to interpret")
    body = next((b.text for b in response.content if b.type == "text"), None)
    try:
        data = json.loads(body or "")
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise LlmUnavailable("the model's answer could not be read")
    return data


# --- items -> intents --------------------------------------------------------


def _decimal(value) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def to_intents(items: list, now: datetime, settings: AppSettings) -> tuple[list, list[str]]:
    """Deterministic conversion of the model's items. Returns (intents,
    problems); every date and timestamp is computed here."""
    today = days.logical_day(now, settings)
    local_now = now.astimezone(settings.tz)
    intents, problems = [], []

    for item in items:
        kind = item.get("kind")
        day = None
        if item.get("date"):
            try:
                day = date.fromisoformat(item["date"])
            except ValueError:
                problems.append(f"Couldn't read the date “{item['date']}”.")
                continue
        elif item.get("day_offset"):
            day = today + timedelta(days=int(item["day_offset"]))
        if day is not None and day > today:
            problems.append("That is in the future; only today and past days can be logged.")
            continue
        if day == today:
            day = None

        when = {"day": day} if day else {}
        clock = parse_clock(item.get("time") or "")
        if clock and kind != "sleep":
            if day:
                at = datetime.combine(day, clock, tzinfo=settings.tz)
            else:
                at = datetime.combine(local_now.date(), clock, tzinfo=settings.tz)
                if at > local_now + timedelta(minutes=30):
                    at -= timedelta(days=1)
            when["at"] = at
        text = (item.get("text") or "").strip()

        if kind == "tracker":
            if item.get("tracker") in (None, NONE):
                problems.append("One item had no tracker.")
                continue
            value = _decimal(item.get("value"))
            intents.append(TrackerLog(tracker=item["tracker"], value=value,
                                      mode="set" if item.get("replace") else "auto", note=text, **when))
        elif kind == "expense":
            amount = _decimal(item.get("amount"))
            if amount is None or item.get("category") in (None, NONE):
                problems.append("An expense needs an amount and a category.")
                continue
            intents.append(ExpenseLog(amount=amount, category=item["category"],
                                      currency=(item.get("currency") or None), note=text, **when))
        elif kind == "sleep":
            def clock_of(value):
                return "now" if value == "now" else parse_clock(value or "")

            bed, wake = clock_of(item.get("bedtime")), clock_of(item.get("wake_time"))
            if not bed and not wake:
                problems.append("Sleep needs a bedtime or a wake time.")
                continue
            if day and "now" in (bed, wake):
                problems.append("A past night needs clock times.")
                continue
            intents.append(sleep_intent(bed, wake, day, now, settings))
        elif kind == "workout":
            kind_of = item.get("workout_type")
            intents.append(WorkoutLog(type="other" if kind_of in (None, NONE) else kind_of,
                                      duration_min=item.get("duration_min"), notes=text, **when))
        elif kind == "note":
            if text:
                intents.append(DayNoteLog(note=text, **when))
        elif kind == "tags":
            tags = [t for t in (item.get("tags") or []) if str(t).strip()]
            if tags:
                intents.append(DayTagLog(tags=tags, **when))
        else:
            problems.append("One item was of an unknown kind.")
    return chain_sleep(intents), problems
