import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse

from tracking import days, metrics
from tracking.models import DayNote, DayTag, Entry, Expense, SleepSession, Workout


@pytest.fixture
def client(client, owner):
    client.force_login(owner)
    return client


@pytest.fixture
def seeded():
    call_command("seed", days=40, verbosity=0)


def events(response):
    return json.loads(response["HX-Trigger"])


def test_everything_requires_login(db):
    from django.test import Client

    anonymous = Client()
    for name in ("web:today", "web:expenses", "web:habits"):
        response = anonymous.get(reverse(name))
        assert response.status_code == 302 and "/login/" in response["Location"]
    assert anonymous.post(reverse("web:log"), {"kind": "tracker", "tracker": "coffee"}).status_code == 302
    assert not Entry.objects.exists()
    assert anonymous.get(reverse("login")).status_code == 200


def test_health_check_needs_no_login_and_checks_the_database(db, monkeypatch):
    from types import SimpleNamespace

    from django.db import OperationalError
    from django.test import Client

    response = Client().get("/healthz/")
    assert response.status_code == 200 and response.content == b"ok"

    def unreachable():
        raise OperationalError("connection refused")

    monkeypatch.setattr("config.health.connection", SimpleNamespace(cursor=unreachable))
    assert Client().get("/healthz/").status_code == 503


@pytest.mark.parametrize(
    "path",
    ["/", "/trends/", "/trends/?range=1y", "/habits/", "/expenses/", "/expenses/?range=7d&category=food&q=a",
     "/expenses/?sort=amount_base&dir=asc&page=2", "/sleep/?range=90d", "/exercise/?type=legs",
     "/exercise/?range=custom&start=2026-10-01&end=2026-10-20", "/expenses/?range=custom&start=bad",
     "/t/coffee/", "/t/mood/?range=1y", "/t/screen/", "/t/read/", "/insights/", "/settings/"],
)
def test_pages_render_with_data(client, seeded, path):
    assert client.get(path).status_code == 200


def test_pages_render_with_no_data_at_all(client):
    today = days.today()
    for path in ["/", "/trends/", "/habits/", "/expenses/", "/sleep/", "/exercise/", "/t/coffee/",
                 f"/day/{today}/"]:
        assert client.get(path).status_code == 200, path


def _row(html, name):
    """The log-list row for `name` in the Today list."""
    today = html.split('id="today-title"')[1]
    return next(chunk for chunk in today.split('<div class="logrow') if f">{name}</a>" in chunk)


def test_home_separates_unlogged_from_zero(client):
    client.post("/log/", {"kind": "tracker", "tracker": "alcohol", "value": "0"})
    html = client.get("/").content.decode()
    assert "missing" in _row(html, "Coffee") and "missing" not in _row(html, "Alcohol")
    assert "Not logged yet" in html  # the sleep tile says so in words


def test_home_lists_what_yesterday_is_missing(client):
    yesterday = (days.today() - timedelta(days=1)).isoformat()
    html = client.get("/").content.decode()
    left = html.split('id="left-title"')[1].split('id="today-title"')[0]
    assert "Coffee" in left and f'value="{yesterday}"' in left
    client.post("/log/", {"kind": "tracker", "tracker": "coffee", "value": "2", "day": yesterday})
    left = client.get("/").content.decode().split('id="left-title"')[1].split('id="today-title"')[0]
    assert ">Coffee<" not in left


def test_home_week_strip_and_tiles(client):
    today = days.today()
    client.post("/log/", {"kind": "expense", "amount": "12.50", "category": "food"})
    client.post("/log/", {"kind": "workout", "type": "legs", "duration": "45"})
    html = client.get("/").content.decode()
    monday = today - timedelta(days=today.weekday())
    assert f'href="/day/{monday.isoformat()}/"' in html and 'aria-current="date"' in html
    assert f"Week {today.isocalendar().week}" in html
    assert "$12.50" in html and "1 of 4" in html and "45m today" in html


def test_home_counter_stepper_sets_the_total(client):
    today = days.today()
    client.post("/log/", {"kind": "tracker", "tracker": "coffee", "value": "1"})
    client.post("/log/", {"kind": "tracker", "tracker": "coffee", "value": "1"})
    assert 'value": "1"' in _row(client.get("/").content.decode(), "Coffee")  # minus goes to 1
    client.post("/log/", {"kind": "tracker", "tracker": "coffee", "value": "1", "mode": "set"})
    assert metrics.value("tracker:coffee", today) == 1


def test_sleep_from_the_log_list(client):
    today = days.today()
    assert "refresh" in events(client.post("/log/", {"kind": "sleep", "range": "23:30-7:10"}))
    assert metrics.value("sleep.duration", today) == 460
    assert events(client.post("/log/", {"kind": "sleep"}))["toast"]["error"]
    assert "Bedtime" in events(client.post("/log/", {"kind": "sleep", "now": "bed"}))["toast"]["message"]


def test_tasks(client):
    from tracking.models import Task

    today = days.today()
    assert events(client.post("/tasks/", {"text": "  "}))["toast"]["error"]
    ev = events(client.post("/tasks/", {"text": "call the bank tomorrow"}))
    assert ev["toast"]["message"] == "Added: Call the bank · due tomorrow"
    task = Task.objects.get()
    assert task.due == today + timedelta(days=1)
    assert "Call the bank" in client.get("/").content.decode()

    client.post(f"/tasks/{task.pk}/toggle/")
    task.refresh_from_db()
    assert task.done_at is not None
    html = client.get("/").content.decode()
    assert 'class="task done"' in html  # done today stays visible, struck through
    client.post(f"/tasks/{task.pk}/toggle/")
    task.refresh_from_db()
    assert task.done_at is None
    client.post(f"/tasks/{task.pk}/delete/")
    assert not Task.objects.exists()


def test_day_view_and_future_dates(client, seeded):
    today = days.today()
    assert client.get(f"/day/{today - timedelta(days=3)}/").status_code == 200
    assert client.get(f"/day/{today + timedelta(days=3)}/").status_code == 302
    assert client.get("/day/nonsense/").status_code == 404


def test_one_tap_logging_and_undo(client):
    today = days.today()
    response = client.post("/log/", {"kind": "tracker", "tracker": "coffee", "value": "1"})
    ev = events(response)
    assert ev["toast"]["message"] == "Coffee +1" and "refresh" in ev
    assert metrics.value("tracker:coffee", today) == 1
    assert "Undone" in events(client.post(f"/undo/{ev['toast']['op']}/"))["toast"]["message"]
    assert metrics.value("tracker:coffee", today) is None
    assert events(client.post(f"/undo/{ev['toast']['op']}/"))["toast"]["error"]


def test_forms_log_each_kind_and_backfill(client):
    past = days.today() - timedelta(days=5)
    day = past.isoformat()
    posts = [
        {"kind": "tracker", "tracker": "screen", "value": "3h20", "day": day},
        {"kind": "tracker", "tracker": "mood", "value": "4", "day": day},
        {"kind": "expense", "amount": "12.50", "category": "food", "note": "milk", "day": day},
        {"kind": "workout", "type": "legs", "duration": "1h15", "notes": "squats", "day": day},
        {"kind": "sleep", "bed": "23:30", "wake": "07:10", "day": day},
        {"kind": "note", "note": "a day", "day": day},
        {"kind": "tags", "tags": "#sick, travel", "day": day},
    ]
    for data in posts:
        assert "error" not in events(client.post("/log/", data))["toast"], data
    assert metrics.value("tracker:screen", past) == 200
    assert metrics.value("tracker:mood", past) == 4
    assert metrics.value("expenses.total", past) == Decimal("12.50")
    assert metrics.value("exercise.minutes", past) == 75
    assert metrics.value("sleep.duration", past) == 460
    assert DayNote.objects.get(day=past).note == "a day"
    assert DayTag.objects.filter(day=past).count() == 2
    assert client.get(f"/day/{day}/").status_code == 200


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "tracker", "tracker": "mood", "value": "9"},
        {"kind": "tracker", "tracker": "screen", "value": "ages"},
        {"kind": "expense", "amount": "abc", "category": "food"},
        {"kind": "workout", "type": "legs", "duration": "long"},
        {"kind": "sleep"},
        {"kind": "nope"},
    ],
)
def test_bad_input_gives_an_error_toast_and_saves_nothing(client, data):
    assert events(client.post("/log/", data))["toast"]["error"] is True
    assert not Entry.objects.exists() and not Expense.objects.exists() and not Workout.objects.exists()


def test_palette_preview_then_confirm(client):
    text = "2 coffees, spent 12.5 food milk, legs 60"
    preview = client.post("/palette/parse/", {"text": text}).content.decode()
    assert "Coffee +2" in preview and "$12.50 · Groceries · milk" in preview and "Workout: Legs" in preview
    assert not Entry.objects.exists()  # preview never saves

    ev = events(client.post("/palette/confirm/", {"text": text}))
    assert "palette-close" in ev and "refresh" in ev
    assert Entry.objects.count() == 1 and Expense.objects.count() == 1 and Workout.objects.count() == 1


def test_palette_explains_problems_and_does_not_save(client):
    assert "Couldn&#x27;t understand" in client.post("/palette/parse/", {"text": "blargh"}).content.decode()
    assert "at most 5" in client.post("/palette/parse/", {"text": "mood 9"}).content.decode()
    ev = events(client.post("/palette/confirm/", {"text": "coffee, mood 9"}))
    assert ev["toast"]["error"] and "palette-close" not in ev
    assert not Entry.objects.exists()
    assert "Type what happened" in client.post("/palette/parse/", {"text": ""}).content.decode()


def test_palette_navigation(client):
    assert "Go to Sleep" in client.post("/palette/parse/", {"text": "go sl"}).content.decode()
    response = client.post("/palette/confirm/", {"text": "go expenses"})
    assert json.loads(response["HX-Location"])["path"] == "/expenses/"


def test_inline_edit_delete_and_undo(client):
    client.post("/log/", {"kind": "expense", "amount": "10", "category": "food", "note": "old"})
    e = Expense.objects.get()
    assert 'name="amount"' in client.get(f"/row/expense/{e.pk}/?edit=1").content.decode()

    bad = client.post(f"/row/expense/{e.pk}/edit/", {"day": e.day, "amount": "-1", "category": "food"})
    assert "more than zero" in bad.content.decode() and "HX-Trigger" not in bad

    ok = client.post(f"/row/expense/{e.pk}/edit/",
                     {"day": e.day, "amount": "22", "currency": "CAD", "category": "transport", "note": "new"})
    e.refresh_from_db()
    assert (e.amount, e.category.slug, e.note) == (Decimal("22.00"), "transport", "new")
    assert "$22.00" in ok.content.decode()

    ev = events(client.post(f"/row/expense/{e.pk}/delete/"))
    assert not Expense.objects.exists()
    client.post(f"/undo/{ev['toast']['op']}/")
    assert Expense.objects.get().note == "new"


def test_editing_sleep_and_duration_rows(client):
    day = (days.today() - timedelta(days=2)).isoformat()
    client.post("/log/", {"kind": "sleep", "bed": "23:00", "wake": "07:00", "day": day})
    s = SleepSession.objects.get()
    client.post(f"/row/sleep/{s.pk}/edit/", {"bedtime": f"{s.day - timedelta(days=1)}T22:00", "wake_time": f"{s.day}T07:00"})
    s.refresh_from_db()
    assert s.duration_min == 540

    client.post("/log/", {"kind": "tracker", "tracker": "screen", "value": "200"})
    entry = Entry.objects.get()
    client.post(f"/row/entry/{entry.pk}/edit/", {"day": entry.day, "value": "2h", "note": ""})
    entry.refresh_from_db()
    assert entry.value == 120


def test_csv_export_matches_filters(client, seeded):
    response = client.get("/export/expense.csv?range=90d&category=housing")
    assert response["Content-Type"].startswith("text/csv")
    lines = response.content.decode().strip().splitlines()
    assert lines[0] == "day,amount,currency,amount_cad,category,note,recurring,source"
    assert len(lines) > 1 and all(",housing," in line for line in lines[1:])

    coffee = client.get("/export/entry.csv?range=30d&tracker=coffee").content.decode().strip().splitlines()
    assert len(coffee) > 5 and all(",coffee," in line for line in coffee[1:])
    assert client.get("/export/nope.csv").status_code == 404


# --- palette with the Claude tier ------------------------------------------------


@pytest.fixture
def claude(settings, monkeypatch):
    from parsing import llm
    from parsing.tests.fakes import FakeClaude, item

    fake = FakeClaude([item("tracker", tracker="coffee", value=2), item("expense", amount=14.5, category="eating-out")],
                      unparsed="what a week")
    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kwargs: fake)
    return fake


def test_palette_asks_claude_only_on_enter_and_saves_on_the_second_enter(client, claude):
    from parsing.models import Draft

    text = "had two coffees and bought lunch for 14.50, what a week"
    typing = client.post("/palette/parse/", {"text": text}).content.decode()
    assert "let Claude interpret" in typing and claude.requests == []  # no call per keystroke

    first = client.post("/palette/confirm/", {"text": text})
    html = first.content.decode()
    assert first["HX-Retarget"] == "#palette-preview" and "HX-Trigger" not in first
    assert "Coffee +2" in html and "$14.50 · Eating out" in html and "Interpreted by Claude" in html
    assert "Not logged: “what a week”" in html
    assert len(claude.requests) == 1 and not Entry.objects.exists()
    draft = Draft.objects.get()
    assert f'name="draft" value="{draft.pk}"' in html

    second = client.post("/palette/confirm/", {"text": text, "draft": draft.pk})
    ev = events(second)
    assert "palette-close" in ev and ev["toast"]["message"] == "Coffee +2; $14.50 · Eating out"
    assert len(claude.requests) == 1  # saved from the draft, not a second interpretation
    assert Entry.objects.count() == 1 and Expense.objects.count() == 1 and not Draft.objects.exists()


def test_palette_reinterprets_if_the_text_changed_after_the_preview(client, claude):
    from parsing.models import Draft

    client.post("/palette/confirm/", {"text": "had two coffees and lunch"})
    stale = Draft.objects.get()
    response = client.post("/palette/confirm/", {"text": "had two coffees and a big lunch", "draft": stale.pk})
    assert response["HX-Retarget"] == "#palette-preview" and len(claude.requests) == 2
    assert not Entry.objects.exists()


def test_palette_still_uses_the_grammar_first(client, claude):
    assert "palette-close" in events(client.post("/palette/confirm/", {"text": "coffee"}))
    assert claude.requests == [] and Entry.objects.count() == 1


# --- insights -------------------------------------------------------------------


def test_insights_pages(client, seeded):
    html = client.get("/insights/").content.decode()
    assert "No review yet" in html and "Flags from the last 14 days" in html
    assert "ANTHROPIC_API_KEY" in html  # says how to enable Ask my data instead of failing

    ev = events(client.post("/insights/review/"))
    assert ev["toast"]["message"] == "Review written by the built-in template."
    html = client.get("/insights/").content.decode()
    assert "Try next week:" in html and "Written by the built-in template" in html

    from insights.models import WeeklyReview

    week = WeeklyReview.objects.get().week_start
    assert client.get(f"/insights/week/{week}/").status_code == 200
    assert client.get("/insights/week/2020-01-06/").status_code == 404

    html = client.get("/insights/correlations/").content.decode()
    assert "Screen time → Sleep" in html and 'data-chart="{&quot;type&quot;: &quot;scatter&quot;' in html


def test_insights_with_no_data(client):
    assert client.get("/insights/").status_code == 200
    assert "Not enough data yet" in client.get("/insights/correlations/").content.decode()


def test_ask_box(client, settings, monkeypatch):
    from insights import ask
    from insights.tests.test_insights import Asker

    assert "needs a Claude API key" in client.post("/insights/ask/", {"question": "streaks?"}).content.decode()
    settings.ANTHROPIC_API_KEY = "test-key"
    fake = Asker([("habit_streaks", {})], "Your longest streak is 0 days.")
    monkeypatch.setattr(ask.anthropic, "Anthropic", lambda **kwargs: fake)
    html = client.post("/insights/ask/", {"question": "What are my streaks?"}).content.decode()
    assert "Your longest streak is 0 days." in html and "habit_streaks" in html and "1 query" in html


def test_flags_are_on_insights_not_home(client):
    from datetime import timedelta as td

    today = days.today()
    # Small amounts: 20 days of groceries stay under its 500 budget whatever the
    # date, so no budget flag pushes the spike out of Today's top three.
    for i in range(20, 0, -1):
        client.post("/log/", {"kind": "expense", "amount": 10 + i % 9, "category": "food", "day": (today - td(days=i)).isoformat()})
    client.post("/log/", {"kind": "expense", "amount": "900", "category": "shopping", "day": (today - td(days=2)).isoformat()})
    assert "Spending spike on" in client.get("/insights/").content.decode()
    assert "Spending spike on" not in client.get("/").content.decode()  # flags moved off Home


# --- settings ---------------------------------------------------------------------


def test_settings_general(client):
    from tracking.models import AppSettings

    assert client.get("/settings/").status_code == 200
    data = {"timezone": "Europe/Berlin", "day_start": "05:00", "reminder_time": "22:15", "weekly_review_time": "17:00",
            "reminder_enabled": "on"}
    assert "error" not in events(client.post("/settings/general/", data))["toast"]
    s = AppSettings.load()
    assert (s.timezone, s.day_start.hour, s.reminder_time.minute) == ("Europe/Berlin", 5, 15)
    assert s.reminder_enabled and not s.weekly_review_enabled  # unchecked box turns it off

    bad = events(client.post("/settings/general/", {**data, "timezone": "Mars/Olympus"}))
    assert bad["toast"]["error"] and AppSettings.load().timezone == "Europe/Berlin"
    assert events(client.post("/settings/general/", {**data, "day_start": "soon"}))["toast"]["error"]


def test_astana_and_tenge(client):
    from tracking import fmt
    from tracking.models import AppSettings

    data = {"timezone": "Astana", "day_start": "04:00", "reminder_time": "21:00", "weekly_review_time": "18:00",
            "currency": "KZT"}
    assert "error" not in events(client.post("/settings/general/", data))["toast"]
    s = AppSettings.load()
    assert (s.timezone, s.default_currency) == ("Asia/Almaty", "KZT")  # "Astana" is saved as its IANA zone
    assert 'label="Kazakhstan: Astana, Almaty"' in client.get("/settings/").content.decode()

    assert fmt.money(2500) == "₸2,500" and fmt.money("12.5", "USD") == "12.50 USD"
    client.post("/log/", {"kind": "expense", "amount": "2500", "category": "food"})
    page = client.get("/").content.decode()
    assert "₸2,500" in page and "$2,500" not in page
    assert 'data-symbol="₸" data-places="0"' in page  # what the charts format with


def test_settings_goals(client):
    from tracking.models import Goal

    Goal.objects.all().delete()
    ev = events(client.post("/settings/goals/", {"metric": "sleep.duration", "comparator": "gte", "target": "7h30", "period": "day"}))
    assert ev["toast"]["message"] == "Goal added: At least 7h 30m sleep a day"
    goal = Goal.objects.get()
    assert (goal.metric, goal.target, goal.comparator) == ("sleep.duration", 450, "gte")
    client.post("/settings/goals/", {"metric": "exercise.sessions", "comparator": "gte", "target": "4", "period": "week", "name": "Gym"})
    assert Goal.objects.get(name="Gym").period == "week"
    assert "At least 7h 30m" in client.get("/").content.decode()  # shows up on Today straight away

    for bad in ({"metric": "nope", "comparator": "gte", "target": "1", "period": "day"},
                {"metric": "tracker:coffee", "comparator": "gte", "target": "lots", "period": "day"},
                {"metric": "sleep.duration", "comparator": "gte", "target": "soon", "period": "day"},
                {"metric": "tracker:coffee", "comparator": "eq", "target": "1", "period": "day"}):
        assert events(client.post("/settings/goals/", bad))["toast"]["error"]
    assert Goal.objects.count() == 2

    client.post(f"/settings/goals/{goal.pk}/toggle/")
    goal.refresh_from_db()
    assert not goal.is_active and "At least 7h 30m" not in client.get("/").content.decode()
    client.post(f"/settings/goals/{goal.pk}/delete/")
    assert Goal.objects.count() == 1


def test_settings_budgets_keep_history(client):
    from datetime import date
    from decimal import Decimal

    from tracking.models import Budget, ExpenseCategory

    food, fun = ExpenseCategory.objects.get(slug="food"), ExpenseCategory.objects.get(slug="fun")
    data = {f"budget-{c.pk}": "" for c in ExpenseCategory.objects.all()}
    data |= {f"budget-{food.pk}": "$650", f"budget-{fun.pk}": ""}
    assert events(client.post("/settings/budgets/", data))["toast"]["message"] == "Budgets saved."

    today = days.today()
    current = {b["category"].slug: b["budget"] for b in metrics.budgets(today)}
    assert current == {"food": Decimal(650)}  # every other category was blanked
    # Earlier months still see the old budgets.
    earlier = {b["category"].slug: b["budget"] for b in metrics.budgets(date(2020, 6, 15))}
    assert earlier["food"] == Decimal(500) and earlier["fun"] == Decimal(150)
    assert Budget.objects.filter(category=food).count() == 2

    client.post("/settings/budgets/", data)  # saving again changes nothing
    assert Budget.objects.filter(category=food).count() == 2
    assert events(client.post("/settings/budgets/", {**data, f"budget-{food.pk}": "plenty"}))["toast"]["error"]
    assert 'value="650"' in client.get("/settings/").content.decode()


def test_settings_trackers_need_no_code(client):
    from tracking.models import Tracker

    ev = events(client.post("/settings/trackers/", {"name": "Water", "kind": "counter", "unit": "glasses", "aliases": "h2o, Glass of water"}))
    assert "Tracker added: Water" in ev["toast"]["message"]
    client.post("/settings/trackers/", {"name": "Focus", "kind": "scale", "min": "1", "max": "10"})
    client.post("/settings/trackers/", {"name": "Deep work", "kind": "duration"})
    assert Tracker.objects.get(slug="focus").max_value == 10 and Tracker.objects.get(slug="deep-work").unit == "min"

    # Usable everywhere immediately: palette, quick log, Today, detail page.
    assert "palette-close" in events(client.post("/palette/confirm/", {"text": "3 h2o, focus 8, deep work 1h30"}))
    today = days.today()
    assert metrics.value("tracker:water", today) == 3 and metrics.value("tracker:deep-work", today) == 90
    assert "Water" in client.get("/").content.decode() and client.get("/t/water/").status_code == 200

    for bad in ({"name": "Water", "kind": "counter"}, {"name": "", "kind": "habit"}, {"name": "X", "kind": "poem"},
                {"name": "Bad scale", "kind": "scale", "min": "5", "max": "1"}):
        assert events(client.post("/settings/trackers/", bad))["toast"]["error"]

    water = Tracker.objects.get(slug="water")
    client.post(f"/settings/trackers/{water.pk}/toggle/")
    assert events(client.post("/log/", {"kind": "tracker", "tracker": "water"}))["toast"]["error"]  # hidden
    assert metrics.value("tracker:water", today) == 3  # history kept


def test_settings_tokens(client):
    from tracking.models import ApiToken

    html = client.post("/settings/tokens/", {"name": "iPhone Shortcuts"}).content.decode()
    token = ApiToken.objects.get()
    raw = html.split('value="')[1].split('"')[0]
    assert ApiToken.lookup(raw) == token and "can't be shown again" in html
    assert raw not in client.get("/settings/").content.decode()  # shown once only
    assert "iPhone Shortcuts" in client.get("/settings/").content.decode()
    assert events(client.post("/settings/tokens/", {"name": ""}))["toast"]["error"]
    client.post(f"/settings/tokens/{token.pk}/revoke/")
    assert not ApiToken.objects.exists()


def test_telegram_is_hidden_until_the_bot_is_configured(client, settings):
    settings.TELEGRAM_BOT_USERNAME = ""
    assert "Connect Telegram" not in client.get("/settings/").content.decode()
    assert events(client.post("/settings/telegram/connect/"))["toast"]["error"]


def test_connecting_telegram_from_settings(client, owner, settings):
    from tracking.models import TelegramLink

    settings.TELEGRAM_BOT_USERNAME = "dash_bot"
    assert "Connect Telegram" in client.get("/settings/").content.decode()
    html = client.post("/settings/telegram/connect/").content.decode()
    raw = html.split("start=")[1].split('"')[0]
    assert "https://t.me/dash_bot?start=" in html and f"/start {raw}" in html
    assert TelegramLink.objects.get().code_hash != raw  # only the hash is kept
    assert client.get("/settings/telegram/").status_code == 204  # still waiting

    TelegramLink.redeem(raw, 4242, "Askar (@askar)")
    html = client.get("/settings/telegram/").content.decode()
    assert "Connected to <strong>Askar (@askar)</strong>" in html
    assert "Askar (@askar)" in client.get("/settings/").content.decode()

    client.post("/settings/telegram/disconnect/")
    assert TelegramLink.for_telegram(4242) is None
    assert "Connect Telegram" in client.get("/settings/").content.decode()
