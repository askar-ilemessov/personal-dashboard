"""Logical-day arithmetic. The only place that knows about the day boundary."""

from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .models import AppSettings


def to_aware(dt: datetime, settings: AppSettings | None = None) -> datetime:
    """Naive datetimes are interpreted in the app timezone."""
    if timezone.is_aware(dt):
        return dt
    settings = settings or AppSettings.load()
    return dt.replace(tzinfo=settings.tz)


def logical_day(dt: datetime, settings: AppSettings | None = None) -> date:
    """The day a moment belongs to: 02:30 on the 15th is still the 14th when
    the boundary is 04:00."""
    settings = settings or AppSettings.load()
    local = to_aware(dt, settings).astimezone(settings.tz)
    start = settings.day_start
    return (local - timedelta(hours=start.hour, minutes=start.minute)).date()


def today(settings: AppSettings | None = None) -> date:
    return logical_day(timezone.now(), settings)


def local_noon(day: date, settings: AppSettings | None = None) -> datetime:
    """Timestamp used for backfilled rows that have a day but no time."""
    settings = settings or AppSettings.load()
    return datetime.combine(day, time(12, 0), tzinfo=settings.tz)


def utc_offset_min(dt: datetime, settings: AppSettings | None = None) -> int:
    settings = settings or AppSettings.load()
    offset = to_aware(dt, settings).astimezone(settings.tz).utcoffset()
    return int(offset.total_seconds() // 60)


def daterange(start: date, end: date):
    """Inclusive."""
    for i in range((end - start).days + 1):
        yield start + timedelta(days=i)
