import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from conftest import local
from insights import ask, facts, review
from insights.models import WeeklyReview
from tracking import logbook
from tracking.logbook import ExpenseLog, SleepLog, TrackerLog, WorkoutLog
from tracking.models import AppSettings, ExpenseCategory, RecurringExpense

TODAY = date(2026, 11, 15)  # a Sunday
MONDAY = date(2026, 11, 9)


def log(*intents):
    logbook.apply(intents, source="seed")


def night(day, hours, bed_hour=23.5):
    """Sleep ending on `day` after `hours`, going to bed at `bed_hour`."""
    bed = local(day.year, day.month, day.day, 0) - timedelta(days=1) + timedelta(hours=bed_hour)
    return SleepLog(bedtime=bed, wake_time=bed + timedelta(hours=hours))


@pytest.fixture
def history():
    """60 days with known structure: more screen time means less sleep the
    following night; mood follows sleep; coffee is unrelated to anything."""
    for i in range(60, 0, -1):
        day = TODAY - timedelta(days=i)
        screen = 120 + (i * 37) % 180  # 120..299 minutes, deterministic
        log(TrackerLog(tracker="screen", value=screen, day=day),
            TrackerLog(tracker="coffee", value=1 + (i * 7) % 3, day=day))
    for i in range(59, -1, -1):
        day = TODAY - timedelta(days=i)
        screen_before = 120 + ((i + 1) * 37) % 180
        hours = 9 - screen_before / 60  # 4h to 7h
        log(night(day, hours), TrackerLog(tracker="mood", value=max(1, min(5, round(hours - 2))), day=day))


# --- correlations ----------------------------------------------------------------


def test_finds_the_planted_relationships_and_not_the_absent_one(history):
    found = {(r.x.key, r.y.key): r for r in facts.correlations(TODAY)}
    screen_sleep = found[("tracker:screen", "sleep.duration")]
    assert screen_sleep.lag == 1 and screen_sleep.result.rho < -0.95 and screen_sleep.reliable
    assert screen_sleep.sentence == "After days with more screen time, you tend to sleep less the next day."
    assert "Strong link (ρ = -1.00 over 60 days), unlikely to be chance." == screen_sleep.detail
    sleep_mood = found[("sleep.duration", "tracker:mood")]
    assert sleep_mood.result.rho > 0.9 and sleep_mood.sentence == "On days with more sleep, mood tends to be higher."
    coffee = found[("sleep.duration", "tracker:coffee")]
    assert not coffee.reliable


def test_too_little_data_is_said_plainly(history):
    exercise = next(r for r in facts.correlations(TODAY) if r.x.key == "exercise.minutes")
    assert exercise.result is None and "nothing to compare" in exercise.detail  # exercise is 0 every day
    alcohol = next(r for r in facts.correlations(TODAY) if r.x.key == "tracker:alcohol")
    assert alcohol.result is None and "0 days have both logged; 14 are needed." in alcohol.detail


def test_other_relationships_only_lists_reliable_new_pairs(history):
    pairs = {(r.x.key, r.y.key) for r in facts.other_relationships(TODAY)}
    assert ("sleep.duration", "tracker:mood") not in pairs  # already one of the standing questions
    assert all(r.reliable for r in facts.other_relationships(TODAY))


def test_correlations_with_no_data_at_all():
    assert all(r.result is None for r in facts.correlations(TODAY))
    assert facts.other_relationships(TODAY) == []
    assert facts.anomalies(TODAY) == []


# --- anomalies -------------------------------------------------------------------


@pytest.fixture
def spending():
    for i in range(40, 0, -1):
        log(ExpenseLog(amount=30 + (i * 13) % 25, category="food", day=TODAY - timedelta(days=i)))


def test_spending_spike_is_flagged_but_rent_is_not(spending):
    log(ExpenseLog(amount="648.99", category="shopping", note="New monitor", day=TODAY - timedelta(days=3)))
    RecurringExpense.objects.create(name="Rent", amount=1650, category=ExpenseCategory.objects.get(slug="housing"),
                                    start_date=TODAY - timedelta(days=5))
    logbook.materialize_recurring(TODAY)
    flags = [f for f in facts.anomalies(TODAY) if f["kind"] == "spending"]
    assert [f["title"] for f in flags] == ["Spending spike on Nov 12"]
    assert "Largest: New monitor ($648.99)." in flags[0]["detail"] and "a typical day is" in flags[0]["detail"]


def test_old_spikes_fall_out_of_the_window(spending):
    log(ExpenseLog(amount=700, category="shopping", day=TODAY - timedelta(days=20)))
    assert not [f for f in facts.anomalies(TODAY) if f["kind"] == "spending"]


def test_short_sleep_runs_and_one_bad_night():
    for i in range(30, 5, -1):
        log(night(TODAY - timedelta(days=i), 7.5))
    log(night(TODAY - timedelta(days=5), 4))  # a single short night is not a run
    log(night(TODAY - timedelta(days=4), 7.5))
    assert not [f for f in facts.anomalies(TODAY) if f["kind"] == "sleep"]
    for i, hours in [(3, 5), (2, 4.5), (1, 5.5)]:
        log(night(TODAY - timedelta(days=i), hours))
    [flag] = [f for f in facts.anomalies(TODAY) if f["kind"] == "sleep"]
    assert flag["title"] == "3 short nights in a row"
    assert flag["detail"] == "Nov 12–Nov 14: under 6h each night, 5h on average."


def test_high_counter_low_mood_budget_and_gaps():
    for i in range(30, 0, -1):
        day = TODAY - timedelta(days=i)
        if i in (2, 3):
            continue  # two days with nothing logged
        log(TrackerLog(tracker="coffee", value=7 if i == 6 else 1 + i % 2, day=day),
            TrackerLog(tracker="mood", value=2 if i in (9, 10, 11) else 4, day=day))
    log(ExpenseLog(amount=260, category="eating-out", day=TODAY))  # budget is 250
    titles = {f["kind"]: f["title"] for f in facts.anomalies(TODAY)}
    assert titles["high"] == "Unusually high coffee on Nov 9"
    assert titles["low"] == "Low mood 3 days running"
    assert titles["budget"] == "Over budget: Eating out"
    assert titles["gap"] == "Nothing logged on 2 of the last 7 days"


# --- weekly review ---------------------------------------------------------------


@pytest.fixture
def two_weeks():
    for i in range(13, -1, -1):
        day = TODAY - timedelta(days=i)
        this_week = day >= MONDAY
        log(night(day, 7.5 if this_week else 6), TrackerLog(tracker="mood", value=4 if this_week else 3, day=day),
            TrackerLog(tracker="coffee", value=1 if this_week else 3, day=day),
            ExpenseLog(amount=20 if this_week else 40, category="food", day=day))
        if this_week and i % 2:
            log(TrackerLog(tracker="read", value=1, day=day))
    log(WorkoutLog(type="legs", duration_min=60, day=MONDAY))


def test_weekly_facts(two_weeks):
    week = facts.weekly(MONDAY, TODAY)
    rows = {r["key"]: r for r in week["metrics"]}
    assert week["period"] == {"start": "2026-11-09", "end": "2026-11-15", "label": "Nov 9–Nov 15", "days": 7, "complete": True}
    sleep = rows["sleep.duration"]
    assert (sleep["value"], sleep["previous"], sleep["change"], sleep["direction"]) == ("7h 30m", "6h", "25%", "up")
    assert rows["tracker:coffee"]["change"] == "67%" and rows["tracker:coffee"]["direction"] == "down"
    assert rows["expenses.total"]["value"] == "$140.00" and rows["expenses.total"]["previous"] == "$280.00"
    assert rows["tracker:screen"]["value"] == "—" and rows["tracker:screen"]["logged_days"] == 0  # unlogged, not 0
    assert {"name": "Read 20 min", "done": 3, "logged": 3, "days": 7} in week["habits"]
    assert {"name": "Sleep 7h+", "met_days": 7, "logged_days": 7} in week["goals"]
    assert {"name": "Gym 4× a week", "result": "1", "target": "4", "met": False} in week["goals"]
    assert week["workouts"] == {"count": 1, "types": ["legs"]}
    json.dumps(week)  # must be storable as JSON


def test_a_first_week_has_nothing_to_compare_with():
    log(TrackerLog(tracker="coffee", value=2, day=MONDAY))
    row = next(r for r in facts.weekly(MONDAY, TODAY)["metrics"] if r["key"] == "tracker:coffee")
    assert row["value"] == "2" and row["previous"] is None and row["change"] is None


def test_a_week_in_progress_says_so(two_weeks):
    week = facts.weekly(MONDAY, MONDAY + timedelta(days=2))
    assert week["period"]["days"] == 3 and not week["period"]["complete"]
    assert "3 of 7 days so far" in review.write_with_template(week)["summary"]


def test_template_review_is_complete_and_uses_only_computed_numbers(two_weeks):
    week = facts.weekly(MONDAY, TODAY)
    text = review.write_with_template(week)
    assert text["summary"].startswith("Week of Nov 9–Nov 15: Sleep averaged 7h 30m, up 25% from 6h the week before")
    assert 2 <= len(text["observations"]) <= 3 and text["suggestion"]
    everything = " ".join([text["summary"], *text["observations"], text["suggestion"]])
    assert review.unsupported_numbers(everything, week) == set()


def test_template_review_with_no_data():
    text = review.write_with_template(facts.weekly(MONDAY, TODAY))
    assert "not enough was logged" in text["summary"] and len(text["observations"]) >= 1 and text["suggestion"]


def test_number_check():
    week = {"metrics": [{"value": "7h 08m", "change": "13%", "previous": "$1,412.50"}], "label": "Nov 9–Nov 15"}
    assert review.unsupported_numbers("Sleep was 7h 08m, up 13%, and you spent $1,412.50 by Nov 15.", week) == set()
    assert review.unsupported_numbers("Sleep was 7h 10m, about 14% up.", week) == {"10", "14"}
    assert review.unsupported_numbers("You slept roughly 7.1 hours.", week) == {"7.1"}
    assert review.unsupported_numbers("No figures here, just words like three and seven.", week) == set()


class Writer:
    """Fake Claude for the review: returns each given answer in turn."""

    def __init__(self, *answers, stop_reason="end_turn"):
        self.answers, self.requests, self.stop_reason = list(answers), [], stop_reason
        self.messages = self

    def with_options(self, **kw):
        return self

    def create(self, **request):
        self.requests.append(request)
        body = self.answers.pop(0)
        body = body if isinstance(body, str) else json.dumps(body)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=body)])


GOOD = {"summary": "A steadier week: sleep averaged 7h 30m, up 25% from 6h the week before.",
        "observations": ["Coffee was down 67%.", "You met Sleep 7h+ on 7 of 7 logged days."],
        "suggestion": "Keep the same bedtime next week."}
BAD = {**GOOD, "summary": "Sleep averaged about 7.5 hours, a 26% improvement."}


def test_claude_words_the_facts(two_weeks):
    writer = Writer(GOOD)
    made = review.generate(MONDAY, today=TODAY, client=writer)
    assert made.written_by == "claude" and made.summary == GOOD["summary"] and len(made.observations) == 2
    [request] = writer.requests
    assert request["model"] == "claude-sonnet-5-5" and request["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "thinking", "tool_choice"} & set(request)
    sent = request["messages"][0]["content"]
    assert '"value": "7h 30m"' in sent and "_delta" not in sent  # formatted facts only
    assert "_delta" not in json.dumps(made.facts)


def test_invented_numbers_get_one_retry_then_the_template(two_weeks):
    writer = Writer(BAD, GOOD)
    assert review.generate(MONDAY, today=TODAY, client=writer).written_by == "claude"
    retry = writer.requests[1]["messages"]
    assert "not in the facts: 26, 7.5" in retry[-1]["content"] and retry[1]["role"] == "assistant"

    stubborn = Writer(BAD, BAD)
    made = review.generate(MONDAY, today=TODAY, force=True, client=stubborn)
    assert made.written_by == "template" and "7.5 hours" not in made.summary and len(stubborn.requests) == 2


@pytest.mark.parametrize("writer", [Writer("not json"), Writer(GOOD, stop_reason="refusal"),
                                    Writer({"summary": "x"}), Writer(GOOD, stop_reason="max_tokens")])
def test_unusable_answers_fall_back_to_the_template(two_weeks, writer):
    assert review.generate(MONDAY, today=TODAY, client=writer).written_by == "template"


def test_generate_is_idempotent_unless_forced(two_weeks):
    first = review.generate(MONDAY, today=TODAY)
    assert first.written_by == "template"  # no key, no client
    assert review.generate(date(2026, 11, 12), today=TODAY).pk == first.pk and WeeklyReview.objects.count() == 1
    assert review.generate(MONDAY, today=TODAY, force=True, client=Writer(GOOD)).written_by == "claude"
    assert WeeklyReview.objects.count() == 1


def test_due_only_on_sunday_evening_and_only_once():
    assert review.due(local(2026, 11, 14, 19, 0)) is None  # Saturday
    assert review.due(local(2026, 11, 15, 17, 59)) is None  # Sunday, too early
    assert review.due(local(2026, 11, 15, 18, 0)) == MONDAY
    made = review.generate(MONDAY, today=TODAY)
    assert review.due(local(2026, 11, 15, 20, 0)) == MONDAY  # written but not sent yet
    WeeklyReview.objects.filter(pk=made.pk).update(sent_at=local(2026, 11, 15, 18, 5))
    assert review.due(local(2026, 11, 15, 20, 0)) is None
    s = AppSettings.load()
    s.weekly_review_enabled = False
    s.save()
    assert review.due(local(2026, 11, 22, 19, 0)) is None


# --- ask my data -------------------------------------------------------------------


class Asker:
    """Fake Claude for the tool loop: a script of tool calls, then an answer."""

    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []
        self.messages = self

    def with_options(self, **kw):
        return self

    def create(self, **request):
        self.requests.append(json.loads(json.dumps(request, default=lambda o: "<block>")))
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=turn)])
        blocks = [SimpleNamespace(type="thinking", thinking="")]
        blocks += [SimpleNamespace(type="tool_use", id=f"call{i}", name=name, input=args) for i, (name, args) in enumerate(turn)]
        return SimpleNamespace(stop_reason="tool_use", content=blocks)


RANGE = {"start": "2026-11-09", "end": "2026-11-15"}


def test_tool_definitions_are_strict_and_closed():
    assert {t["name"] for t in ask.TOOLS} == set(ask.FUNCTIONS)
    for tool in ask.TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] is True and schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
    assert "sql" not in json.dumps(ask.TOOLS).lower()


def test_query_functions(two_weeks):
    out = json.loads(ask.run_tool("summarize", {"metric": "sleep.duration", **RANGE})[0])
    assert out["average_per_logged_day"] == "7h 30m" and out["days_logged"] == 7
    out = json.loads(ask.run_tool("summarize", {"metric": "tracker:screen", **RANGE})[0])
    assert out["days_logged"] == 0 and "Nothing was logged" in out["note"]
    out = json.loads(ask.run_tool("compare_periods", {"metric": "expenses.total", "start_a": "2026-11-02",
                                                      "end_a": "2026-11-08", "start_b": "2026-11-09", "end_b": "2026-11-15"})[0])
    assert out["period_b"]["total"] == "$140.00" and out["change"] == "Period B's total is 50% lower than period A's."
    out = json.loads(ask.run_tool("find_days", {"metric": "sleep.duration", "comparison": "lt", "value": 420,
                                                "start": "2026-11-02", "end": "2026-11-15"})[0])
    assert out["matching_days"] == 7 and out["days"][0]["value"] == "6h"
    out = json.loads(ask.run_tool("expense_breakdown", RANGE)[0])
    assert out["total"] == "$140.00" and out["categories"] == [{"category": "Groceries", "amount": "$140.00", "share": "100%"}]
    out = json.loads(ask.run_tool("list_entries", {"kind": "workouts", "search": "", **RANGE})[0])
    assert out["rows"] == [{"day": "2026-11-09", "type": "legs", "duration": "1h", "notes": ""}]
    out = json.loads(ask.run_tool("weekday_averages", {"metric": "tracker:coffee", **RANGE})[0])
    assert out["weekdays"][0] == {"weekday": "Monday", "days_logged": 1, "average": "1"}
    out = json.loads(ask.run_tool("get_series", {"metric": "tracker:screen", **RANGE})[0])
    assert all(d["value"] is None for d in out["days"]) and "not zero" in out["note"]


@pytest.mark.parametrize(
    "name,args,fragment",
    [
        ("summarize", {"metric": "users", **RANGE}, "Unknown metric"),
        ("summarize", {"metric": "tracker:coffee", "start": "yesterday", "end": "2026-11-15"}, "must be dates"),
        ("summarize", {"metric": "tracker:coffee", "start": "2026-11-15", "end": "2026-11-09"}, "after end"),
        ("get_series", {"metric": "tracker:coffee", "start": "2000-01-01", "end": "2026-11-15"}, "too long"),
        ("find_days", {"metric": "tracker:coffee", "comparison": "like", "value": 1, **RANGE}, "comparison"),
        ("correlation", {"metric_x": "tracker:coffee", "metric_y": "tracker:mood", "lag_days": 30, **RANGE}, "lag_days"),
        ("list_entries", {"kind": "users", "search": "", **RANGE}, "kind must be"),
        ("drop_tables", {}, "Unknown tool"),
        ("summarize", None, "Unknown metric"),
    ],
)
def test_bad_tool_arguments_are_refused_with_a_reason(name, args, fragment):
    output, ok = ask.run_tool(name, args)
    assert not ok and fragment in output


def test_ask_runs_the_tool_loop_and_reports_how(two_weeks):
    client = Asker([("summarize", {"metric": "sleep.duration", **RANGE}), ("habit_streaks", {})],
                   "You averaged 7h 30m of sleep over 7 logged days this week.")
    answer = ask.ask("How did I sleep this week?", client=client)
    assert answer.text.startswith("You averaged 7h 30m") and not answer.error and answer.unverified == []
    assert [s["tool"] for s in answer.steps] == ["summarize", "habit_streaks"]
    first, second = client.requests
    assert first["model"] == "claude-sonnet-5-5" and "tool_choice" not in first and len(first["tools"]) == 9
    assert "Question: How did I sleep this week?" in first["messages"][0]["content"]
    assert second["messages"][1]["role"] == "assistant"  # the model's own turn goes back unchanged
    results = second["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["call0", "call1"]  # all results in one message
    assert '"average_per_logged_day": "7h 30m"' in results[0]["content"] and results[0]["is_error"] is False


def test_figures_no_query_returned_are_called_out(two_weeks):
    client = Asker([("summarize", {"metric": "sleep.duration", **RANGE})], "About 7.6 hours a night, roughly 53 hours in all.")
    assert ask.ask("How did I sleep this week?", client=client).unverified == ["53", "7.6"]


def test_tool_errors_go_back_to_the_model(two_weeks):
    client = Asker([("summarize", {"metric": "sleep", **RANGE})], [("summarize", {"metric": "sleep.duration", **RANGE})], "7h 30m.")
    answer = ask.ask("sleep?", client=client)
    assert [s["ok"] for s in answer.steps] == [False, True] and answer.text == "7h 30m."
    assert client.requests[1]["messages"][2]["content"][0]["is_error"] is True


def test_ask_stops_runaway_loops_and_handles_missing_setup():
    endless = Asker(*[[("habit_streaks", {})]] * 20)
    assert "too many lookups" in ask.ask("streaks?", client=endless).error and len(endless.requests) == ask.MAX_ROUNDS
    assert "API key" in ask.ask("streaks?").error
    assert ask.ask("   ", client=Asker("x")).error
