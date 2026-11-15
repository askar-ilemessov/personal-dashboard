"""Weekly review: summary, two or three observations, one suggestion.

The facts come from insights.facts. Claude words them when an API key is
set; otherwise (or if its text fails the checks) a deterministic template
does. Either way no number reaches the reader that the stats engine did
not compute: Claude's text is rejected if it contains any number that is
not in the facts.
"""

import json
import logging
import re
from datetime import date, datetime

import anthropic
from django.conf import settings as django_settings
from django.utils import timezone

from tracking import days
from tracking.models import AppSettings

from . import facts as facts_module
from .models import WeeklyReview

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Two or three sentences on how the week went."},
        "observations": {"type": "array", "items": {"type": "string"},
                         "description": "Two or three things worth noticing, one sentence each."},
        "suggestion": {"type": "string", "description": "One concrete thing to try next week."},
    },
    "required": ["summary", "observations", "suggestion"],
    "additionalProperties": False,
}

SYSTEM = """You write the weekly review for one person's private life-tracking app. They log sleep, mood, energy, coffee, alcohol, screen time, exercise, habits and spending, and read this review on Sunday evening.

You are given facts that the app computed from their data. Your job is only to put them into plain words.

Rules:
- Use only the facts provided. Do not add outside knowledge about the person.
- Every number you write must appear in the facts exactly as written there (for example "7h 08m", "13%", "$412.50"). Do not calculate, round, convert, add up or estimate anything, and do not introduce any number that is not in the facts. If you want to express a quantity that is not there, say it in words without a figure or leave it out.
- "summary": two or three sentences on the overall shape of the week.
- "observations": two or three single sentences, each about something specific and worth noticing: a notable change from the week before, a flag, a relationship, a goal or a habit. Prefer what is surprising or actionable over restating the summary.
- "suggestion": one concrete, small thing to try next week, tied to something in the facts.
- Spending and exercise have "active_days" (days with any spending or any workout); a day without either is a real zero, not a gap.
- When a metric's logged_days is lower than days, the week is only partly logged; say so rather than treating the average as the whole week. A missing value means it was not logged, not that it was zero.
- A relationship in the facts is a correlation in their own history, not proof of cause; word it as a tendency.
- Write to the person as "you", in plain, direct sentences. No greetings, no headings, no bullet characters, no praise for its own sake."""


class Rejected(Exception):
    pass


# --- number check ----------------------------------------------------------------

_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text)}


def unsupported_numbers(text: str, facts: dict) -> set[str]:
    """Numbers in `text` that do not appear anywhere in the facts."""
    allowed = numbers_in(json.dumps(facts, ensure_ascii=False))
    return numbers_in(text) - allowed


def _public(facts: dict) -> dict:
    """Facts without internal helper fields (keys starting with "_")."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if not k.startswith("_")}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(facts)


# --- writers -----------------------------------------------------------------------


def write_with_claude(facts: dict, client=None) -> dict:
    client = client or anthropic.Anthropic(api_key=django_settings.ANTHROPIC_API_KEY)
    messages = [{"role": "user", "content": "Facts for this week:\n" + json.dumps(_public(facts), indent=1, ensure_ascii=False)}]
    for _attempt in range(2):
        try:
            response = client.with_options(timeout=90.0, max_retries=1).messages.create(
                model=django_settings.INSIGHTS_MODEL,
                max_tokens=8000,
                system=SYSTEM,
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=messages,
            )
        except anthropic.APIError as e:
            raise Rejected(f"API error: {type(e).__name__}") from None
        if response.stop_reason in ("refusal", "max_tokens"):
            raise Rejected(f"stopped with {response.stop_reason}")
        body = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(body)
            text = " ".join([data["summary"], *data["observations"], data["suggestion"]])
        except (ValueError, KeyError, TypeError):
            raise Rejected("unreadable answer") from None
        extra = unsupported_numbers(text, _public(facts))
        if not extra and 1 <= len(data["observations"]) <= 4:
            return {"summary": data["summary"].strip(), "observations": [o.strip() for o in data["observations"]][:3],
                    "suggestion": data["suggestion"].strip()}
        # One chance to fix it; thinking blocks go back unchanged with the rest of the turn.
        messages += [
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": "These numbers are not in the facts: " + ", ".join(sorted(extra)) +
                                        ". Rewrite the review using only numbers that appear in the facts, "
                                        "with two or three observations."},
        ]
    raise Rejected("used numbers that are not in the facts")


def _change_phrase(row: dict) -> str:
    if not row.get("change") or row["direction"] == "flat":
        return ""
    return f", {row['direction']} {row['change']} from {row['previous']} the week before"


def write_with_template(facts: dict) -> dict:
    """Deterministic wording. Plainer than Claude's, but always available."""
    rows = {r["key"]: r for r in facts["metrics"]}
    period = facts["period"]
    logged = [r for r in facts["metrics"] if r["logged_days"]]

    parts = []
    for key, label in [("sleep.duration", "Sleep averaged"), ("tracker:mood", "mood averaged"),
                       ("exercise.minutes", "exercise came to"), ("expenses.total", "spending came to")]:
        row = rows.get(key)
        if row and row.get("active_days", row["logged_days"]):
            parts.append(f"{label} {row['value']}{_change_phrase(row)}")
    summary = f"Week of {period['label']}"
    summary += ": " + "; ".join(parts) + "." if parts else ": not enough was logged to summarise."
    if not period["complete"]:
        summary += f" The week is not over yet ({period['days']} of 7 days so far)."

    observations = [f"{f['title']}. {f['detail']}" for f in facts["flags"][:2]]
    movers = sorted((r for r in logged if r.get("_delta") is not None and abs(r["_delta"]) >= 0.1),
                    key=lambda r: -abs(r["_delta"]))
    for row in movers:
        if len(observations) >= 3:
            break
        observations.append(f"{row['name']} was {row['direction']} {row['change']}: {row['value']} against {row['previous']} the week before.")
    for g in facts["goals"]:
        if len(observations) >= 3:
            break
        if "met_days" in g and g["logged_days"]:
            observations.append(f"{g['name']}: met on {g['met_days']} of {g['logged_days']} logged days.")
    if len(observations) < 2 and facts["relationships"]:
        observations.append(facts["relationships"][0])
    if len(observations) < 2:
        partly = [r["name"] for r in facts["metrics"] if 0 < r["logged_days"] < r["days"]]
        observations.append("Partly logged this week: " + ", ".join(partly) + "." if partly
                            else "Nothing stood out against the week before.")

    kinds = {f["kind"] for f in facts["flags"]}
    missed = [g for g in facts["goals"] if "met_days" in g and g["logged_days"] and g["met_days"] * 2 < g["logged_days"]]
    weak_habit = min((h for h in facts["habits"] if h["logged"]), key=lambda h: h["done"] / h["logged"], default=None)
    if "sleep" in kinds:
        suggestion = "Protect sleep first next week: pick a fixed time to put screens away on work nights and keep it for five nights."
    elif missed:
        suggestion = f"Pick one goal to focus on next week: “{missed[0]['name']}” was met on {missed[0]['met_days']} of {missed[0]['logged_days']} logged days."
    elif "spending" in kinds or "budget" in kinds:
        suggestion = "Before any purchase over your usual daily spend next week, wait a day."
    elif weak_habit and weak_habit["done"] * 2 < weak_habit["logged"]:
        suggestion = f"Attach “{weak_habit['name']}” to something you already do every day, and try it for three days in a row."
    elif any(r["logged_days"] < r["days"] for r in facts["metrics"] if r["key"].startswith("tracker:")):
        suggestion = "Log the evening items before bed each day next week, so the averages cover the whole week."
    else:
        suggestion = "Keep the same routine next week and see whether the numbers hold."
    return {"summary": summary, "observations": observations[:3], "suggestion": suggestion}


# --- generate, deliver -----------------------------------------------------------------


def generate(week_start: date | None = None, today: date | None = None, force: bool = False, client=None) -> WeeklyReview:
    """Create (or return) the review for the week containing `week_start`."""
    today = today or days.today()
    start = facts_module.week_start(week_start or today)
    existing = WeeklyReview.objects.filter(week_start=start).first()
    if existing and not force:
        return existing

    facts = facts_module.weekly(start, today)
    written_by = "template"
    text = None
    if django_settings.ANTHROPIC_API_KEY or client:
        try:
            text = write_with_claude(facts, client=client)
            written_by = "claude"
        except Rejected as e:
            log.warning("Weekly review fell back to the template: %s", e)
    text = text or write_with_template(facts)

    review, _ = WeeklyReview.objects.update_or_create(
        week_start=start,
        defaults={"facts": _public(facts), "written_by": written_by, "created_at": timezone.now(), **text},
    )
    return review


def due(now: datetime | None = None) -> date | None:
    """The Monday of the week whose review should be sent now, if any:
    Sunday at or after the configured time, and not sent yet."""
    settings = AppSettings.load()
    if not settings.weekly_review_enabled:
        return None
    local = (now or timezone.now()).astimezone(settings.tz)
    if local.weekday() != 6 or local.time() < settings.weekly_review_time:
        return None
    start = facts_module.week_start(local.date())
    if WeeklyReview.objects.filter(week_start=start, sent_at__isnull=False).exists():
        return None
    return start
