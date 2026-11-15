from datetime import date
from decimal import Decimal

import anthropic
import httpx2
import pytest

from conftest import local
from parsing import llm, parser
from parsing.models import Draft
from tracking import logbook, metrics
from tracking.logbook import DayNoteLog, DayTagLog, ExpenseLog, SleepLog, TrackerLog, WorkoutLog
from tracking.models import AppSettings, Entry, ExchangeRate, ExpenseCategory, Tracker

from .fakes import FakeClaude, item

NOW = local(2026, 11, 14, 9, 0)  # Saturday morning
D = date(2026, 11, 14)


def convert(*items):
    intents, problems = llm.to_intents(list(items), NOW, AppSettings.load())
    assert problems == [], problems
    return intents


# --- the request -----------------------------------------------------------


def test_schema_only_allows_what_exists():
    Tracker.objects.create(slug="water", name="Water", kind="counter")
    Tracker.objects.filter(slug="junk").update(is_active=False)
    s = llm.schema(list(Tracker.objects.filter(is_active=True)), list(ExpenseCategory.objects.filter(is_active=True)))
    props = s["properties"]["items"]["items"]
    assert "water" in props["properties"]["tracker"]["enum"] and "junk" not in props["properties"]["tracker"]["enum"]
    assert props["properties"]["tracker"]["enum"][-1] == "none"
    assert "eating-out" in props["properties"]["category"]["enum"]
    # Structured outputs need every field required and no extras.
    assert set(props["required"]) == set(props["properties"]) and props["additionalProperties"] is False
    assert s["required"] == ["items", "unparsed"] and s["additionalProperties"] is False


def test_request_shape():
    client = FakeClaude([item("tracker", tracker="coffee", value=1)])
    llm.call("had a coffee", NOW, AppSettings.load(), client=client)
    [request] = client.requests
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "thinking", "tool_choice"} & set(request)  # rejected or unneeded on this model
    assert request["max_tokens"] >= 2000  # thinking counts against it
    user = request["messages"][0]["content"]
    assert "Sat 2026-11-14 09:00" in user and "Fri 2026-11-13 = -1" in user and "had a coffee" in user
    # The system prompt carries the configuration and nothing that changes per request.
    system = request["system"]
    assert "- coffee: Coffee" in system and "also called: drink, drinks, beer, wine" in system
    assert "- eating-out: Eating out" in system and "2026" not in system


def test_before_four_am_the_model_is_told_the_logical_day():
    client = FakeClaude([item("tracker", tracker="coffee", value=1)])
    llm.call("coffee", local(2026, 11, 14, 2, 0), AppSettings.load(), client=client)
    assert "Fri 2026-11-13 = 0" in client.requests[0]["messages"][0]["content"]


# --- items -> intents: what varied sentences turn into ------------------------


def test_counts_and_zero():
    # "had two coffees this morning and a beer with dinner; no junk food today"
    assert convert(
        item("tracker", tracker="coffee", value=2),
        item("tracker", tracker="alcohol", value=1, text="with dinner"),
        item("tracker", tracker="junk", value=0, replace=True),
    ) == [
        TrackerLog(tracker="coffee", value=Decimal(2)),
        TrackerLog(tracker="alcohol", value=Decimal(1), note="with dinner"),
        TrackerLog(tracker="junk", value=Decimal(0), mode="set"),
    ]


def test_expenses_with_currency_and_backfill():
    # "bought lunch for 14.50, and paid 40 US dollars for a taxi yesterday"
    assert convert(
        item("expense", amount=14.5, category="eating-out", text="lunch"),
        item("expense", amount=40, currency="USD", category="transport", text="taxi", day_offset=-1),
    ) == [
        ExpenseLog(amount=Decimal("14.5"), category="eating-out", note="lunch"),
        ExpenseLog(amount=Decimal(40), currency="USD", category="transport", note="taxi", day=date(2026, 11, 13)),
    ]


def test_sleep_dates_are_worked_out_in_python():
    # "went to bed at half eleven, up at ten past seven"
    assert convert(item("sleep", bedtime="23:30", wake_time="07:10")) == [
        SleepLog(bedtime=local(2026, 11, 13, 23, 30), wake_time=local(2026, 11, 14, 7, 10))]
    # "going to bed" / "just woke up"
    assert convert(item("sleep", bedtime="now")) == [SleepLog(bedtime=NOW)]
    assert convert(item("sleep", wake_time="now")) == [SleepLog(wake_time=NOW)]
    # "on Tuesday I slept from 1am to 8"
    assert convert(item("sleep", bedtime="01:00", wake_time="08:00", day_offset=-4)) == [
        SleepLog(bedtime=local(2026, 11, 10, 1, 0), wake_time=local(2026, 11, 10, 8, 0))]


def test_workouts_habits_scales_and_durations():
    # "ran for 35 minutes, did my reading, skipped meditation, feeling great, 3 and a half hours of screens"
    assert convert(
        item("workout", workout_type="cardio", duration_min=35, text="run"),
        item("workout", duration_min=60, text="climbing"),
        item("tracker", tracker="read", value=1),
        item("tracker", tracker="meditate", value=0),
        item("tracker", tracker="mood", value=5),
        item("tracker", tracker="screen", value=210),
    ) == [
        WorkoutLog(type="cardio", duration_min=35, notes="run"),
        WorkoutLog(type="other", duration_min=60, notes="climbing"),
        TrackerLog(tracker="read", value=Decimal(1)),
        TrackerLog(tracker="meditate", value=Decimal(0)),
        TrackerLog(tracker="mood", value=Decimal(5)),
        TrackerLog(tracker="screen", value=Decimal(210)),
    ]


def test_notes_tags_times_and_dates():
    assert convert(
        item("tags", tags=["sick", " "]),
        item("note", text="stayed in bed all day"),
        item("tracker", tracker="coffee", value=1, time="07:30"),
        item("tracker", tracker="coffee", value=1, time="15:00"),  # 3pm hasn't happened yet today
        item("tracker", tracker="alcohol", value=2, date="2026-11-01", time="21:00"),
        item("note", text=""), item("tags"),  # empty ones are dropped
    ) == [
        DayTagLog(tags=["sick"]),
        DayNoteLog(note="stayed in bed all day"),
        TrackerLog(tracker="coffee", value=Decimal(1), at=local(2026, 11, 14, 7, 30)),
        TrackerLog(tracker="coffee", value=Decimal(1), at=local(2026, 11, 13, 15, 0)),
        TrackerLog(tracker="alcohol", value=Decimal(2), day=date(2026, 11, 1), at=local(2026, 11, 1, 21, 0)),
    ]


def test_day_offset_counts_from_the_logical_day():
    [intent] = llm.to_intents([item("tracker", tracker="coffee", value=1, day_offset=-1)],
                              local(2026, 11, 14, 2, 0), AppSettings.load())[0]
    assert intent.day == date(2026, 11, 12)


@pytest.mark.parametrize(
    "bad,fragment",
    [
        (item("tracker", value=1), "no tracker"),
        (item("expense", amount=12), "amount and a category"),
        (item("expense", category="food"), "amount and a category"),
        (item("sleep"), "bedtime or a wake time"),
        (item("sleep", bedtime="now", day_offset=-2), "clock times"),
        (item("tracker", tracker="coffee", value=1, day_offset=1), "future"),
        (item("tracker", tracker="coffee", value=1, date="next week"), "Couldn't read the date"),
        ({"kind": "poem"}, "unknown kind"),
    ],
)
def test_unusable_items_are_reported_not_guessed(bad, fragment):
    intents, problems = llm.to_intents([bad], NOW, AppSettings.load())
    assert intents == [] and fragment in problems[0]


# --- the two tiers together ---------------------------------------------------


def test_grammar_handles_what_it_can_without_calling_the_model():
    client = FakeClaude()
    result = parser.interpret("2 coffees, spent 12.5 food", now=NOW, client=client)
    assert result.ok and result.tier == "grammar" and client.requests == []


def test_sentences_fall_through_to_the_model():
    client = FakeClaude([item("tracker", tracker="coffee", value=2), item("expense", amount=14.5, category="eating-out")],
                        unparsed="what a week")
    result = parser.interpret("had two coffees and bought lunch for 14.50, what a week", now=NOW, client=client)
    assert result.ok and result.tier == "llm" and len(client.requests) == 1
    assert result.lines == ["Coffee +2", "$14.50 · Eating out"]
    assert result.ignored == "what a week"
    assert not Entry.objects.exists()  # interpreting never saves


def test_model_output_gets_the_same_validation_as_everything_else():
    result = parser.interpret("my mood is off the charts", now=NOW, client=FakeClaude([item("tracker", tracker="mood", value=9)]))
    assert not result.ok and "at most 5" in result.errors[0]
    result = parser.interpret("forty bucks at the bar", now=NOW,
                              client=FakeClaude([item("expense", amount=40, currency="EUR", category="fun")]))
    assert not result.ok and "exchange rate for EUR" in result.errors[0]
    ExchangeRate.objects.create(currency="EUR", rate=Decimal("1.5"))
    assert parser.interpret("forty euros at the bar", now=NOW,
                            client=FakeClaude([item("expense", amount=40, currency="eur", category="fun")])).ok


def test_nothing_to_log():
    result = parser.interpret("how did I sleep last week?", now=NOW, client=FakeClaude(unparsed="how did I sleep last week?"))
    assert not result.ok and "couldn't find anything to log" in result.errors[0]


@pytest.mark.parametrize(
    "client,fragment",
    [
        (FakeClaude(stop_reason="refusal"), "declined"),
        (FakeClaude(stop_reason="max_tokens"), "too long"),
        (FakeClaude(raw="Sure! Here you go"), "could not be read"),
        (FakeClaude(raw='["not", "an", "object"]'), "could not be read"),
        (FakeClaude(error=anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))),
         "could not reach"),
    ],
)
def test_model_failures_degrade_to_the_grammar_error(client, fragment):
    result = parser.interpret("had a lovely flat white", now=NOW, client=client)
    assert not result.ok and result.tier == "grammar"
    assert "Couldn't understand" in result.errors[0]  # the grammar's own explanation comes first
    assert fragment in result.errors[-1]


def test_without_an_api_key_only_the_grammar_runs(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no API call expected")

    monkeypatch.setattr(llm.anthropic, "Anthropic", boom)
    assert not parser.llm_enabled()
    result = parser.interpret("had a lovely flat white", now=NOW)
    assert not result.ok and len(result.errors) == 1


def test_a_draft_saves_exactly_what_was_previewed_without_a_second_call():
    client = FakeClaude([item("tracker", tracker="coffee", value=2), item("sleep", bedtime="23:30", wake_time="07:10")])
    result = parser.interpret("two coffees, slept half eleven to ten past seven", now=NOW, client=client)
    draft = parser.save_draft(result, "telegram")
    intents = parser.draft_intents(Draft.objects.get(pk=draft.pk), now=NOW)
    assert intents == result.intents and len(client.requests) == 1
    logbook.apply(intents, source="telegram", now=NOW)
    assert metrics.value("tracker:coffee", D) == 2 and metrics.value("sleep.duration", D) == 460

    grammar_draft = parser.save_draft(parser.interpret("mood 4", now=NOW), "web")
    assert grammar_draft.items is None
    assert parser.draft_intents(grammar_draft, now=NOW) == [TrackerLog(tracker="mood", value="4")]
