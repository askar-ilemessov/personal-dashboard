"""Data model.

Conventions (see CLAUDE.md):
- Every logged row carries a logical `day` computed at write time from the
  day boundary, plus the real UTC timestamp where one exists.
- "Not logged" is the absence of a row. An Entry with value 0 means
  "logged, none".
- All writes go through tracking.logbook, never directly.
"""

from datetime import time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone


class Source(models.TextChoices):
    WEB = "web", "Web"
    TELEGRAM = "telegram", "Telegram"
    SHORTCUT = "shortcut", "iOS Shortcut"
    RECURRING = "recurring", "Recurring rule"
    SEED = "seed", "Seed data"
    ADMIN = "admin", "Admin"


def _default_timezone():
    return django_settings.APP_TIMEZONE


class AppSettings(models.Model):
    """Singleton (pk=1). Use AppSettings.load()."""

    timezone = models.CharField(max_length=64, default=_default_timezone)
    day_start = models.TimeField(
        default=time(4, 0), help_text="Logical day boundary. Anything before this counts as the previous day."
    )
    default_currency = models.CharField(max_length=3, default="CAD")
    reminder_time = models.TimeField(default=time(21, 0))
    reminder_enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = verbose_name_plural = "Settings"

    def __str__(self):
        return "Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        ZoneInfo(self.timezone)  # fail loudly on a bad zone name
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def tz(self):
        return ZoneInfo(self.timezone)


class Operation(models.Model):
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


class Tracker(models.Model):
    class Kind(models.TextChoices):
        HABIT = "habit", "Habit (done / not done)"
        COUNTER = "counter", "Counter (adds up over the day)"
        DURATION = "duration", "Duration (minutes, one total per day)"
        SCALE = "scale", "Scale (e.g. 1–5, one value per day)"

    class Aggregation(models.TextChoices):
        SUM = "sum", "Sum of the day's entries"
        LAST = "last", "Single value per day"

    slug = models.SlugField(unique=True)
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

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.aggregation:
            self.aggregation = (
                self.Aggregation.SUM if self.kind == self.Kind.COUNTER else self.Aggregation.LAST
            )
        self.aliases = [a.strip().lower() for a in self.aliases if a.strip()]
        super().save(*args, **kwargs)


class LoggedRow(models.Model):
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


class ExpenseCategory(models.Model):
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=64)
    aliases = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = "expense categories"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.aliases = [a.strip().lower() for a in self.aliases if a.strip()]
        super().save(*args, **kwargs)


class ExchangeRate(models.Model):
    """Manually maintained: 1 unit of `currency` = `rate` units of the default currency."""

    currency = models.CharField(max_length=3, unique=True)
    rate = models.DecimalField(max_digits=12, decimal_places=6)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.currency} × {self.rate}"

    def save(self, *args, **kwargs):
        self.currency = self.currency.upper()
        super().save(*args, **kwargs)


class RecurringExpense(models.Model):
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


class Budget(models.Model):
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
        constraints = [models.UniqueConstraint(fields=["day"], name="one_note_per_day")]

    def __str__(self):
        return f"{self.day} note"


class Tag(models.Model):
    slug = models.SlugField(unique=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.slug


class DayTag(LoggedRow):
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="days")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["day", "tag"], name="one_tag_per_day")]

    def __str__(self):
        return f"{self.day} #{self.tag.slug}"


# --- Goals, tokens ---------------------------------------------------------


class Goal(models.Model):
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


class ApiToken(models.Model):
    """Bearer token for the iOS Shortcuts endpoint. Only the SHA-256 is stored."""

    name = models.CharField(max_length=64)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name
