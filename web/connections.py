"""Connections: where data can come from without typing it. Weather and a
calendar link (shown on Home), Apple Health sleep through an iOS Shortcut
and the API, and Telegram. Settings edits; nothing here is a logged row."""

from django.conf import settings as django_settings
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from tracking.models import ApiToken, AppSettings, TelegramLink

from . import feeds
from .views import _error, _events


def _saved(message: str):
    return _events(refresh={}, toast={"message": message})


def masked(url: str) -> str:
    """A private calendar link, shown without the secret part."""
    if not url:
        return ""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or "calendar"
    return f"{host}/…{url[-6:]}"


def page(request):
    s = AppSettings.load()
    link = TelegramLink.load()
    return render(request, "web/connections.html", {
        "nav": "settings", "settings": s, "calendar": masked(s.calendar_url),
        "api_url": request.build_absolute_uri(reverse("api:log")),
        "has_token": ApiToken.objects.filter(is_active=True).exists(),
        "telegram": link and link.linked, "bot": django_settings.TELEGRAM_BOT_USERNAME,
    })


@require_POST
def weather_search(request):
    try:
        found = feeds.places(request.POST.get("q", ""))
    except feeds.FeedError as e:
        return _error(e)
    return render(request, "web/partials/places.html", {"places": found, "q": request.POST.get("q", "")})


@require_POST
def weather_set(request):
    p = request.POST
    try:
        lat, lon = float(p["lat"]), float(p["lon"])
    except (KeyError, ValueError):
        return _error("Pick a place from the list.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return _error("Pick a place from the list.")
    s = AppSettings.load()
    s.weather_place, s.weather_lat, s.weather_lon = p.get("name", "")[:120], lat, lon
    s.save(update_fields=["weather_place", "weather_lat", "weather_lon"])
    return _saved(f"Weather for {s.weather_place or 'that place'} is on Home.")


@require_POST
def weather_clear(request):
    s = AppSettings.load()
    s.weather_place, s.weather_lat, s.weather_lon = "", None, None
    s.save(update_fields=["weather_place", "weather_lat", "weather_lon"])
    return _saved("Weather removed from Home.")


@require_POST
def calendar_set(request):
    url = feeds.normalize_calendar_url(request.POST.get("url", ""))
    if not url:
        return _error("Paste your calendar's private iCal address.")
    if len(url) > 1000:
        return _error("That link is too long.")
    try:
        feeds.check_calendar(url)
    except feeds.FeedError as e:
        return _error(f"Couldn't read that calendar: {e}")
    s = AppSettings.load()
    s.calendar_url = url
    s.save(update_fields=["calendar_url"])
    return _saved("Calendar connected. Today's events are on Home.")


@require_POST
def calendar_clear(request):
    s = AppSettings.load()
    s.calendar_url = ""
    s.save(update_fields=["calendar_url"])
    return _saved("Calendar disconnected.")
