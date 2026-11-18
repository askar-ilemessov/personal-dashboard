"""Scheduled jobs. `run_due()` is called every minute by the bot and returns
the messages to send; each job records that it ran, so nothing is sent twice.

Recurring expenses are materialised for every account. Reminders and the
weekly review go out on Telegram, so only accounts that linked it get them."""

from datetime import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone

from insights import review
from insights.models import WeeklyReview
from tracking import days, logbook
from tracking.models import AppSettings, TelegramLink
from tracking.owner import acting_as

from . import replies


def _minutes_into_day(clock, settings) -> int:
    """Minutes since the logical day started, so a reminder set for 00:30
    still comes after one set for 23:00."""
    start = settings.day_start
    return ((clock.hour - start.hour) * 60 + clock.minute - start.minute) % (24 * 60)


def reminder_due(now: datetime | None = None) -> bool:
    settings = AppSettings.load()
    if not settings.reminder_enabled:
        return False
    now = now or timezone.now()
    if settings.last_reminder_day == days.logical_day(now, settings):
        return False
    local = now.astimezone(settings.tz)
    return _minutes_into_day(local.time(), settings) >= _minutes_into_day(settings.reminder_time, settings)


def evening_reminder(now: datetime | None = None) -> replies.Reply | None:
    """The reminder to send now, if one is due. Nothing unlogged means no message."""
    now = now or timezone.now()
    if not reminder_due(now):
        return None
    settings = AppSettings.load()
    settings.last_reminder_day = days.logical_day(now, settings)
    settings.save(update_fields=["last_reminder_day"])
    return replies.reminder(settings.last_reminder_day)


def weekly_review(now: datetime | None = None) -> replies.Reply | None:
    start = review.due(now)
    if start is None:
        return None
    reply = replies.review(start)
    WeeklyReview.objects.filter(week_start=start).update(sent_at=timezone.now())
    return reply


def run_due(now: datetime | None = None) -> list[tuple[int, replies.Reply]]:
    """(Telegram user id, message) pairs to send now."""
    for user in get_user_model().objects.filter(is_active=True):
        with acting_as(user):
            logbook.materialize_recurring()
    out = []
    for link in TelegramLink.all_users.select_related("user").filter(telegram_id__isnull=False, user__is_active=True):
        with acting_as(link.user):
            out += [(link.telegram_id, r) for r in (evening_reminder(now), weekly_review(now)) if r]
    return out
