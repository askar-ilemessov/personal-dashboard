"""Data model.

Conventions (see CLAUDE.md):
- Every logged row carries a logical `day` computed at write time from the
  day boundary, plus the real UTC timestamp where one exists.
- "Not logged" is the absence of a row. An Entry with value 0 means
  "logged, none".
- All writes go through tracking.logbook, never directly.
"""

import hashlib
import secrets
from datetime import time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from .owner import OwnedModel


class Source(models.TextChoices):
    WEB = "web", "Web"
    TELEGRAM = "telegram", "Telegram"
    SHORTCUT = "shortcut", "iOS Shortcut"
    RECURRING = "recurring", "Recurring rule"
    SEED = "seed", "Seed data"
    ADMIN = "admin", "Admin"


def _default_timezone():
    return django_settings.APP_TIMEZONE


class AppSettings(OwnedModel):
    """One row per user. Use AppSettings.load() for the acting user's."""

    timezone = models.CharField(max_length=64, default=_default_timezone)
    day_start = models.TimeField(
        default=time(4, 0), help_text="Logical day boundary. Anything before this counts as the previous day."
    )
    default_currency = models.CharField(max_length=3, default="CAD")
    reminder_time = models.TimeField(default=time(21, 0))
    reminder_enabled = models.BooleanField(default=True)
    weekly_review_enabled = models.BooleanField(default=True, help_text="Send the weekly review on Telegram")
    weekly_review_time = models.TimeField(default=time(18, 0), help_text="Sunday, local time")
    last_reminder_day = models.DateField(null=True, blank=True, editable=False)
    # Connections (web/feeds.py). The calendar link is a private URL: anyone
    # with it can read the calendar, so it is never shown back in full.
    weather_place = models.CharField(max_length=120, blank=True)
    weather_lat = models.FloatField(null=True, blank=True)
    weather_lon = models.FloatField(null=True, blank=True)
    calendar_url = models.URLField(max_length=1000, blank=True)
    default_range = models.CharField(
        max_length=8, default="week", help_text="Range Trends and the detail pages open on: week, 30d, 90d or 1y"
    )
    # Onboarding (web/setup_views.py). Blank once finished; accounts not
    # created by an invite never go through it.
    setup_step = models.CharField(max_length=16, blank=True)
    setup_deferred = models.BooleanField(default=False, help_text="Chose “Skip setup”: not sent back to it")
    invited_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = verbose_name_plural = "Settings"
        constraints = [models.UniqueConstraint(fields=["user"], name="one_settings_per_user")]

    def __str__(self):
        return "Settings"

    def save(self, *args, **kwargs):
        ZoneInfo(self.timezone)  # fail loudly on a bad zone name
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create()
        return obj

    @property
    def tz(self):
        return ZoneInfo(self.timezone)


class Operation(OwnedModel):
    """One log action (possibly several rows). The unit of undo and audit.

    `changes` is a list of {"model", "pk", "action", "before"} where action is
    create/update/delete and `before` is a field snapshot (None for create).
    """

    source = models.CharField(max_length=16, choices=Source.choices)
    raw_text = models.TextField(blank=True, help_text="Original text or command, if any")
    changes = models.JSONField(default=list, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(default=timezone.now)
    undone_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"#{self.pk} {self.source} ({len(self.changes)} changes)"


# --- Configurable trackers -------------------------------------------------


class Tracker(OwnedModel):
    class Kind(models.TextChoices):
        HABIT = "habit", "Habit (done / not done)"
        COUNTER = "counter", "Counter (adds up over the day)"
        DURATION = "duration", "Duration (minutes, one total per day)"
        SCALE = "scale", "Scale (e.g. 1–5, one value per day)"

    class Aggregation(models.TextChoices):
        SUM = "sum", "Sum of the day's entries"
        LAST = "last", "Single value per day"

    slug = models.SlugField()
    name = models.CharField(max_length=64)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    aggregation = models.CharField(
        max_length=8, choices=Aggregation.choices, blank=True, help_text="Leave blank to derive from kind"
    )
    unit = models.CharField(max_length=16, blank=True)
    min_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    aliases = models.JSONField(default=list, blank=True, help_text="Extra names the parser accepts")
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    weekly_target = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="Habits: days a week to aim for (7 = every day). Blank means every day."
    )

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["user", "slug"], name="unique_tracker_slug")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.aggregation:
            self.aggregation = (
                self.Aggregation.SUM if self.kind == self.Kind.COUNTER else self.Aggregation.LAST
            )
        self.aliases = [a.strip().lower() for a in self.aliases if a.strip()]
        super().save(*args, **kwargs)


class LoggedRow(OwnedModel):
    day = models.DateField(db_index=True)
    source = models.CharField(max_length=16, choices=Source.choices)
    operation = models.ForeignKey(
        Operation, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TimedRow(LoggedRow):
    occurred_at = models.DateTimeField()
    utc_offset_min = models.SmallIntegerField(default=0, help_text="Local UTC offset when logged")

    class Meta:
        abstract = True


class Entry(TimedRow):
    tracker = models.ForeignKey(Tracker, on_delete=models.PROTECT, related_name="entries")
    value = models.DecimalField(max_digits=10, decimal_places=2)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name_plural = "entries"
        ordering = ["-day", "-occurred_at", "-id"]
        indexes = [models.Index(fields=["tracker", "day"])]

    def __str__(self):
        return f"{self.day} {self.tracker.slug}={self.value}"


# --- Expenses --------------------------------------------------------------


class ExpenseCategory(OwnedModel):
    slug = models.SlugField()
    name = models.CharField(max_length=64)
    aliases = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = "expense categories"
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["user", "slug"], name="unique_category_slug")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.aliases = [a.strip().lower() for a in self.aliases if a.strip()]
        super().save(*args, **kwargs)


class ExchangeRate(OwnedModel):
    """Manually maintained: 1 unit of `currency` = `rate` units of the default currency."""

    currency = models.CharField(max_length=3)
    rate = models.DecimalField(max_digits=12, decimal_places=6)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "currency"], name="unique_rate_currency")]

    def __str__(self):
        return f"{self.currency} × {self.rate}"

    def save(self, *args, **kwargs):
        self.currency = self.currency.upper()
        super().save(*args, **kwargs)


class RecurringExpense(OwnedModel):
    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        YEARLY = "yearly", "Yearly"

    name = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="CAD")
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)
    frequency = models.CharField(max_length=8, choices=Frequency.choices, default=Frequency.MONTHLY)
    start_date = models.DateField(help_text="First charge; later charges keep this day of month/week")
    next_due = models.DateField(null=True, blank=True, help_text="Leave blank to start at start_date")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.amount} {self.currency} {self.frequency})"

    def save(self, *args, **kwargs):
        if self.next_due is None:
            self.next_due = self.start_date
        super().save(*args, **kwargs)


class Expense(TimedRow):
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="CAD")
    amount_base = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Amount in the default currency at logging time"
    )
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    note = models.CharField(max_length=255, blank=True)
    recurring = models.ForeignKey(
        RecurringExpense, null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses"
    )

    class Meta:
        ordering = ["-day", "-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["recurring", "day"],
                condition=models.Q(recurring__isnull=False),
                name="one_charge_per_rule_per_day",
            )
        ]

    def __str__(self):
        return f"{self.day} {self.amount} {self.currency} {self.category.slug}"


class Budget(OwnedModel):
    """Monthly budget for a category. The row with the latest effective_from
    on or before a month applies, so changing a budget keeps history."""

    category = models.ForeignKey(ExpenseCategory, on_delete=models.CASCADE, related_name="budgets")
    monthly_amount = models.DecimalField(max_digits=10, decimal_places=2)
    effective_from = models.DateField()

    class Meta:
        ordering = ["category", "-effective_from"]
        constraints = [
            models.UniqueConstraint(fields=["category", "effective_from"], name="one_budget_per_start")
        ]

    def __str__(self):
        return f"{self.category.slug}: {self.monthly_amount}/mo from {self.effective_from}"


# --- Sleep, exercise, daily context ----------------------------------------


class SleepSession(LoggedRow):
    """`day` is the day you woke up. While only bedtime is known it is a
    provisional guess (bedtime's logical day + 1)."""

    bedtime = models.DateTimeField(null=True, blank=True)
    wake_time = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-day", "-id"]

    def __str__(self):
        return f"{self.day} sleep {self.duration_min or '?'} min"

    @property
    def duration_min(self):
        if self.bedtime and self.wake_time:
            return int((self.wake_time - self.bedtime).total_seconds() // 60)
        return None


class Workout(TimedRow):
    class Type(models.TextChoices):
        PUSH = "push", "Push"
        PULL = "pull", "Pull"
        LEGS = "legs", "Legs"
        CARDIO = "cardio", "Cardio"
        OTHER = "other", "Other"

    type = models.CharField(max_length=8, choices=Type.choices)
    duration_min = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-day", "-occurred_at", "-id"]

    def __str__(self):
        return f"{self.day} {self.type} {self.duration_min or '?'} min"


class DayNote(LoggedRow):
    note = models.TextField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "day"], name="one_note_per_day")]

    def __str__(self):
        return f"{self.day} note"


class NoSpendDay(LoggedRow):
    """"Nothing spent" for a day, so a day with no expenses can be told apart
    from a day nobody logged. Ignored once that day has an expense."""

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "day"], name="one_no_spend_per_day")]

    def __str__(self):
        return f"{self.day} nothing spent"


class Tag(OwnedModel):
    slug = models.SlugField()

    class Meta:
        ordering = ["slug"]
        constraints = [models.UniqueConstraint(fields=["user", "slug"], name="unique_tag_slug")]

    def __str__(self):
        return self.slug


class DayTag(LoggedRow):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="days")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["day", "tag"], name="one_tag_per_day")]

    def __str__(self):
        return f"{self.day} #{self.tag.slug}"


# --- Goals, tokens ---------------------------------------------------------


class Goal(OwnedModel):
    """A limit or target on a metric key (see tracking.metrics), e.g.
    tracker:coffee <= 2 per day, sleep.duration >= 420 per day,
    exercise.sessions >= 4 per week. Category budgets live in Budget."""

    class Comparator(models.TextChoices):
        AT_MOST = "lte", "At most"
        AT_LEAST = "gte", "At least"

    class Period(models.TextChoices):
        DAY = "day", "Per day"
        WEEK = "week", "Per week"
        MONTH = "month", "Per month"

    name = models.CharField(max_length=64)
    metric = models.CharField(max_length=64)
    comparator = models.CharField(max_length=3, choices=Comparator.choices)
    target = models.DecimalField(max_digits=10, decimal_places=2)
    period = models.CharField(max_length=8, choices=Period.choices, default=Period.DAY)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def is_met(self, value: Decimal | None) -> bool | None:
        if value is None:
            return None
        return value <= self.target if self.comparator == self.Comparator.AT_MOST else value >= self.target


class ApiToken(OwnedModel):
    """Bearer token for the iOS Shortcuts endpoint. Only the SHA-256 is stored."""

    name = models.CharField(max_length=64)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name

    @staticmethod
    def digest(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, name: str) -> tuple["ApiToken", str]:
        """Create a token. The raw value is returned once and never stored."""
        raw = secrets.token_urlsafe(32)
        return cls.objects.create(name=name, token_hash=cls.digest(raw)), raw

    @classmethod
    def lookup(cls, raw: str) -> "ApiToken | None":
        return cls.all_users.filter(token_hash=cls.digest(raw), is_active=True).select_related("user").first()


class TelegramLink(OwnedModel):
    """Connects an account to one Telegram user, so the bot knows whose data a
    message is for. Linking: Settings issues a one-time code (only its SHA-256
    is stored), the person opens t.me/<bot>?start=<code>, and the bot redeems
    it for whoever sent it. A Telegram user belongs to at most one account."""

    CODE_VALID = timedelta(minutes=15)

    telegram_id = models.BigIntegerField(null=True, blank=True, unique=True)
    name = models.CharField(max_length=128, blank=True, help_text="Telegram name when linked")
    code_hash = models.CharField(max_length=64, blank=True, editable=False)
    code_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    linked_at = models.DateTimeField(null=True, blank=True)
    # Bot UI state: the live menu message (edited in place) and an open question
    # ("How much for Groceries?") whose answer is the next reply. See bot/panel.py.
    panel_message_id = models.BigIntegerField(null=True, blank=True, editable=False)
    panel_screen = models.CharField(max_length=32, blank=True, editable=False)
    prompt_message_id = models.BigIntegerField(null=True, blank=True, editable=False)
    prompt = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user"], name="one_telegram_link_per_user")]

    def __str__(self):
        return self.name or str(self.telegram_id or "not linked")

    @property
    def linked(self) -> bool:
        return self.telegram_id is not None

    @classmethod
    def load(cls) -> "TelegramLink | None":
        return cls.objects.first()

    @classmethod
    def new_code(cls) -> str:
        """A fresh one-time code for the acting user; replaces any earlier one."""
        raw = secrets.token_urlsafe(24)  # 32 characters, all allowed in a /start parameter
        link, _ = cls.objects.get_or_create()
        link.code_hash = ApiToken.digest(raw)
        link.code_expires_at = timezone.now() + cls.CODE_VALID
        link.save(update_fields=["code_hash", "code_expires_at"])
        return raw

    @classmethod
    def redeem(cls, raw: str, telegram_id: int, name: str) -> "TelegramLink | None":
        """Link `telegram_id` to the account that issued `raw`. Moves the
        Telegram user off any account it was linked to before."""
        from django.db import transaction

        with transaction.atomic():
            link = (cls.all_users.select_for_update().select_related("user")
                    .filter(code_hash=ApiToken.digest(raw), code_expires_at__gt=timezone.now(),
                            user__is_active=True).first())
            if link is None:
                return None
            cls.all_users.filter(telegram_id=telegram_id).exclude(pk=link.pk).update(
                telegram_id=None, name="", linked_at=None)
            link.telegram_id, link.name, link.linked_at = telegram_id, name[:128], timezone.now()
            link.code_hash, link.code_expires_at = "", None
            link.save()
            return link

    @classmethod
    def for_telegram(cls, telegram_id: int) -> "TelegramLink | None":
        return cls.all_users.select_related("user").filter(telegram_id=telegram_id, user__is_active=True).first()

    def unlink(self):
        self.telegram_id, self.name, self.linked_at = None, "", None
        self.save(update_fields=["telegram_id", "name", "linked_at"])


class Task(OwnedModel):
    """A to-do on Home. Not a logged row: tasks are edited directly and
    are not part of metrics or undo history."""

    title = models.CharField(max_length=200)
    due = models.DateField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = [models.F("due").asc(nulls_last=True), "created_at"]

    def __str__(self):
        return self.title
