"""Data from outside the app: the weather (Open-Meteo, no account or key)
and today's events from a calendar's private iCalendar link.

Both are fetched by the server, cached, and never fatal: any failure
leaves the card out. A calendar link comes from the user, so it is only
fetched over https, from hosts that resolve to public addresses (checked
again on every redirect), with a size cap and a short timeout."""

import hashlib
import ipaddress
import json
import logging
import socket
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache

log = logging.getLogger(__name__)

TIMEOUT = 5  # seconds
MAX_BYTES = 3_000_000
WEATHER_TTL = 30 * 60
CALENDAR_TTL = 15 * 60
USER_AGENT = "personal-dashboard (+https://github.com)"


class FeedError(Exception):
    """Shown to the user: plain language."""


# --- Fetching ------------------------------------------------------------------------


def _public_host(host: str | None) -> None:
    if not host:
        raise FeedError("That link has no host name.")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FeedError(f"Couldn't find {host}. Check the link.") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FeedError("That link points at a private network address.")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme != "https":
            raise FeedError("The calendar link redirected to an insecure address.")
        _public_host(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, user_supplied: bool = False) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise FeedError("Only https:// links work.")
    if user_supplied:
        _public_host(parsed.hostname)
    opener = urllib.request.build_opener(_CheckedRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            data = response.read(MAX_BYTES + 1)
    except FeedError:
        raise
    except Exception as e:  # network errors, HTTP errors, timeouts
        raise FeedError("Couldn't load it right now. Check the link, or try again later.") from e
    if len(data) > MAX_BYTES:
        raise FeedError("That is too large to be a calendar link.")
    return data


# --- Weather -------------------------------------------------------------------------

WMO = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm", 99: "Thunderstorm",
}


def _deg(v) -> str:
    n = round(float(v))
    return f"{'−' if n < 0 else ''}{abs(n)}°"


def places(query: str) -> list[dict]:
    """Places matching a name, for picking the weather location."""
    query = " ".join(query.split())[:80]
    if len(query) < 2:
        return []
    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": query, "count": 6, "language": "en", "format": "json"})
    data = json.loads(fetch(url))
    out = []
    for r in data.get("results") or []:
        label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
        out.append({"label": label, "name": r.get("name", ""), "lat": r["latitude"], "lon": r["longitude"]})
    return out


def weather(lat: float, lon: float, tz: str) -> dict | None:
    """Now and the rest of today, as finished strings. None if unavailable."""
    key = f"weather:{lat:.2f}:{lon:.2f}:{tz}"
    data = cache.get(key)
    if data is None:
        url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "timezone": tz, "forecast_days": 1,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "hourly": "precipitation_probability", "daily": "temperature_2m_max,temperature_2m_min",
        })
        try:
            data = json.loads(fetch(url))
        except (FeedError, ValueError) as e:
            log.warning("Weather unavailable: %s", e)
            return None
        cache.set(key, data, WEATHER_TTL)
    return summarize_weather(data)


def summarize_weather(data: dict) -> dict | None:
    try:
        current = data["current"]
        now = current["time"][:13]  # "2026-11-23T14"
        rain = ""
        for t, p in zip(data["hourly"]["time"], data["hourly"]["precipitation_probability"]):
            if t[:13] >= now and p is not None and p >= 50:
                rain = "Rain likely now" if t[:13] == now else f"Rain likely from {t[11:16]}"
                break
        return {
            "temp": _deg(current["temperature_2m"]), "feels": _deg(current["apparent_temperature"]),
            "summary": WMO.get(int(current["weather_code"]), "—"),
            "high": _deg(data["daily"]["temperature_2m_max"][0]), "low": _deg(data["daily"]["temperature_2m_min"][0]),
            "rain": rain,
        }
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# --- Calendar ------------------------------------------------------------------------


def normalize_calendar_url(url: str) -> str:
    url = url.strip()
    if url.startswith("webcal://"):  # what iCloud and Outlook hand out
        url = "https://" + url[len("webcal://"):]
    return url


def _calendar(url: str):
    import icalendar

    key = "calendar:" + hashlib.sha256(url.encode()).hexdigest()
    raw = cache.get(key)
    if raw is None:
        raw = fetch(url, user_supplied=True)
        cache.set(key, raw, CALENDAR_TTL)
    try:
        return icalendar.Calendar.from_ical(raw)
    except Exception as e:
        raise FeedError("That link didn't return a calendar. Use the private iCal (.ics) address.") from e


def check_calendar(url: str) -> None:
    """Raise FeedError unless `url` is a readable calendar."""
    _calendar(url)


def events_on(url: str, day: date, tz: ZoneInfo, now: datetime | None = None) -> list[dict] | None:
    """`day`'s events, all-day ones first, as finished strings. None if the
    calendar can't be read right now."""
    import recurring_ical_events

    try:
        cal = _calendar(url)
        found = recurring_ical_events.of(cal).at(day)
    except FeedError as e:
        log.warning("Calendar unavailable: %s", e)
        return None
    except Exception:
        log.exception("Calendar could not be expanded")
        return None
    now = now or datetime.now(tz)
    out = []
    for ev in found:
        start = ev.get("DTSTART").dt if ev.get("DTSTART") else None
        end = ev.get("DTEND").dt if ev.get("DTEND") else None
        title = str(ev.get("SUMMARY") or "Busy")
        where = str(ev.get("LOCATION") or "")
        if isinstance(start, datetime):
            start = (start if start.tzinfo else start.replace(tzinfo=tz)).astimezone(tz)
            if isinstance(end, datetime):
                end = (end if end.tzinfo else end.replace(tzinfo=tz)).astimezone(tz)
            else:
                end = start + timedelta(hours=1)
            out.append({"time": f"{start:%H:%M}", "title": title, "where": where, "sort": (1, start),
                        "past": end <= now, "start": start})
        else:
            out.append({"time": "All day", "title": title, "where": where, "sort": (0, None), "past": False,
                        "start": None})
    out.sort(key=lambda e: (e["sort"][0], e["sort"][1] or now))
    upcoming = next((e for e in out if e["start"] and not e["past"]), None)
    for e in out:
        e["next"] = e is upcoming
        del e["sort"]
    return out
