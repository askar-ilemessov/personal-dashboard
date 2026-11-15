import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from tracking import days, metrics
from tracking.models import DayNote, DayTag, Entry, Expense, SleepSession, Workout


@pytest.fixture
def client(client):
    client.force_login(get_user_model().objects.create_user("me", password="x"))
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


def test_today_separates_unlogged_from_zero(client):
    client.post("/log/", {"kind": "tracker", "tracker": "alcohol", "value": "0"})
    html = client.get("/").content.decode()
    unlogged = html.split("Still unlogged today")[1].split('class="cards"')[0]
    assert "Coffee" in unlogged and "Alcohol" not in unlogged
    assert "Not logged" in html  # cards show the gap in words


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
