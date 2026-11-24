"""Connections (weather, calendar), the People usage page, and the guard on
user-supplied calendar links. No test reaches the network: `feeds.fetch`
and DNS are replaced."""

import json
import socket
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tracking import days
from tracking.models import AppSettings, Operation
from web import feeds

TZ = ZoneInfo("America/Toronto")


@pytest.fixture
def client(client, owner):
    client.force_login(owner)
    return client


def events(response):
    return json.loads(response["HX-Trigger"])


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:standup
DTSTART;TZID=America/Toronto:20261102T093000
DTEND;TZID=America/Toronto:20261102T094500
RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR
SUMMARY:Team stand-up
END:VEVENT
BEGIN:VEVENT
UID:lunch
DTSTART:20261123T173000Z
DTEND:20261123T183000Z
SUMMARY:Lunch
LOCATION:Downtown
END:VEVENT
BEGIN:VEVENT
UID:holiday
DTSTART;VALUE=DATE:20261123
DTEND;VALUE=DATE:20261124
SUMMARY:Day off
END:VEVENT
END:VCALENDAR
"""


REAL_FETCH = feeds.fetch


@pytest.fixture
def offline(monkeypatch):
    """feeds.fetch answers from a dict (anything else is a FeedError), and
    DNS is faked: cal.example.com is public, everything else private."""
    answers = {}

    def fake_fetch(url, *, user_supplied=False):
        if user_supplied:
            feeds._public_host(url.split("/")[2])
        for prefix, body in answers.items():
            if url.startswith(prefix):
                return body
        raise feeds.FeedError("Couldn't load it right now.")

    monkeypatch.setattr(feeds, "fetch", fake_fetch)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(0, 0, 0, "", (
        {"cal.example.com": "93.184.216.34", "evil.example.com": "10.0.0.5"}.get(host, "127.0.0.1"), 443))])
    return answers


# --- calendar ------------------------------------------------------------------------


def test_events_on_a_day(offline):
    offline["https://cal.example.com/"] = ICS
    url = "https://cal.example.com/me.ics"
    now = datetime(2026, 11, 23, 11, 0, tzinfo=TZ)
    found = feeds.events_on(url, date(2026, 11, 23), TZ, now=now)
    assert [(e["time"], e["title"]) for e in found] == [("All day", "Day off"), ("09:30", "Team stand-up"), ("12:30", "Lunch")]
    assert found[1]["past"] and found[2]["next"] and found[2]["where"] == "Downtown"
    assert [e["title"] for e in feeds.events_on(url, date(2026, 11, 22), TZ, now=now)] == []  # Sunday: no stand-up


def test_calendar_links_must_be_public_https(offline):
    for url, message in [("http://cal.example.com/x.ics", "Only https"),
                         ("https://evil.example.com/x.ics", "private network"),
                         ("https://localhost/x.ics", "private network")]:
        with pytest.raises(feeds.FeedError, match=message):
            REAL_FETCH(url, user_supplied=True)


def test_redirects_are_checked_too(offline):
    import urllib.request

    handler = feeds._CheckedRedirect()
    req = urllib.request.Request("https://cal.example.com/a.ics")
    with pytest.raises(feeds.FeedError, match="private network"):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/b.ics")
    with pytest.raises(feeds.FeedError, match="insecure"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://cal.example.com/b.ics")


def test_connect_calendar(client, offline):
    offline["https://cal.example.com/"] = ICS
    assert events(client.post("/connections/calendar/", {"url": ""}))["toast"]["error"]
    assert "private network" in events(client.post("/connections/calendar/", {"url": "https://evil.example.com/a.ics"}))["toast"]["message"]
    ev = events(client.post("/connections/calendar/", {"url": "webcal://cal.example.com/me.ics"}))
    assert ev["toast"]["message"] == "Calendar connected. Today's events are on Home."
    s = AppSettings.load()
    assert s.calendar_url == "https://cal.example.com/me.ics"  # webcal:// becomes https://
    page = client.get("/connections/").content.decode()
    assert "cal.example.com/…me.ics" in page and "https://cal.example.com/me.ics" not in page  # never shown in full
    assert "Today's plan" in client.get("/").content.decode()
    client.post("/connections/calendar/clear/")
    assert AppSettings.load().calendar_url == ""


def test_home_survives_an_unreachable_calendar(client, offline):
    s = AppSettings.load()
    s.calendar_url = "https://cal.example.com/gone.ics"
    s.save()
    html = client.get("/").content.decode()
    assert "Couldn't reach your calendar just now." in html


# --- weather -------------------------------------------------------------------------

FORECAST = {
    "current": {"time": "2026-11-23T14:00", "temperature_2m": 2.6, "apparent_temperature": -1.4, "weather_code": 3},
    "hourly": {"time": [f"2026-11-23T{h:02d}:00" for h in range(24)],
               "precipitation_probability": [0] * 17 + [60] * 7},
    "daily": {"temperature_2m_max": [4.2], "temperature_2m_min": [-0.6]},
}


def test_weather_summary():
    w = feeds.summarize_weather(FORECAST)
    assert w == {"temp": "3°", "feels": "−1°", "summary": "Cloudy", "high": "4°", "low": "−1°",
                 "rain": "Rain likely from 17:00"}
    assert feeds.summarize_weather({}) is None


def test_pick_a_place_and_see_the_weather(client, offline):
    offline["https://geocoding-api.open-meteo.com/"] = json.dumps({"results": [
        {"name": "Astana", "admin1": "Astana", "country": "Kazakhstan", "latitude": 51.18, "longitude": 71.45}]}).encode()
    offline["https://api.open-meteo.com/"] = json.dumps(FORECAST).encode()
    html = client.post("/connections/weather/search/", {"q": "astana"}).content.decode()
    assert "Astana, Astana, Kazakhstan" in html and 'value="51.18"' in html
    assert events(client.post("/connections/weather/", {"name": "Astana", "lat": "200", "lon": "1"}))["toast"]["error"]
    client.post("/connections/weather/", {"name": "Astana", "lat": "51.18", "lon": "71.45"})
    assert "Cloudy · feels −1°" in client.get("/").content.decode()
    client.post("/connections/weather/clear/")
    assert "weather-line" not in client.get("/").content.decode()


# --- people --------------------------------------------------------------------------


def test_people_shows_usage_not_values(client, owner, django_user_model):
    from tracking.owner import acting_as

    other = django_user_model.objects.create_user("friend", password="x")
    with acting_as(other):
        from tracking import logbook
        from tracking.logbook import ExpenseLog

        logbook.apply([ExpenseLog(amount="123.45", category="food", note="secret dinner")], source="telegram")
    assert client.get("/people/").status_code == 302  # superusers only
    owner.is_superuser = True
    owner.save()
    html = client.get("/people/").content.decode()
    assert "friend" in html and "Telegram" in html  # where they log from
    assert "123.45" not in html and "secret dinner" not in html  # never what
    row = html.split("<b>friend</b>")[1].split("</tr>")[0]
    assert '<td class="right">1</td>' in row
    assert Operation.all_users.filter(user=other).count() == 1


def test_people_counts_by_the_accounts_own_day(client, owner):
    owner.is_superuser = True
    owner.save()
    today = days.today()
    html = client.get("/people/").content.decode()
    assert f"{today:%a} 0" in html  # the last bar is today, in the account's timezone
    assert (today - timedelta(days=6)).strftime("%a") in html
