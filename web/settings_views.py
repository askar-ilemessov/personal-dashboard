"""Settings screen: general options, goals, budgets, trackers, API tokens.

These edit configuration, not logged data, so they write models directly
rather than going through tracking.logbook."""

from datetime import time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, available_timezones

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from parsing.grammar import parse_duration
from tracking import days, fmt, metrics
from tracking.models import (
    ApiToken,
    AppSettings,
    Budget,
    Expense,
    ExpenseCategory,
    Goal,
    RecurringExpense,
    Tracker,
)

from . import invites
from .views import _error, _events

ADMIN_LINKS = [
    ("Expense categories", "admin:tracking_expensecategory_changelist"),
    ("Recurring expenses", "admin:tracking_recurringexpense_changelist"),
    ("Exchange rates", "admin:tracking_exchangerate_changelist"),
    ("Edit or rename trackers", "admin:tracking_tracker_changelist"),
]


CURRENCIES = ["CAD", "USD", "EUR", "GBP", "AUD", "NZD", "CHF", "JPY", "MXN", "INR"]


class Invalid(ValueError):
    pass


def currency_locked() -> bool:
    """Expenses keep their amount converted at logging time, so the main
    currency can't change once there are any."""
    return Expense.objects.exists() or RecurringExpense.objects.exists()


def _goal_metrics() -> list[metrics.Metric]:
    return [*metrics.catalog(), metrics.get("exercise.sessions")]


def _time(text, label) -> time:
    try:
        return time.fromisoformat(text)
    except (TypeError, ValueError):
        raise Invalid(f"{label} must be a time like 21:00.") from None


def _saved(message: str):
    return _events(refresh={}, toast={"message": message})


def page(request):
    s = AppSettings.load()
    today = days.today(s)
    current = {b["category"].pk: b["budget"] for b in metrics.budgets(today)}
    goals = []
    for g in Goal.objects.all():
        try:
            m = metrics.get(g.metric)
        except (KeyError, Tracker.DoesNotExist):
            m = None
        goals.append({"goal": g, "metric": m, "target": fmt.value(m.fmt, g.target) if m else g.target})
    return render(request, "web/settings.html", {
        "nav": "settings", "settings": s, "timezones": sorted(available_timezones()),
        "currencies": CURRENCIES, "currency_locked": currency_locked(),
        "goals": goals, "goal_metrics": _goal_metrics(),
        "budget_rows": [{"category": c, "amount": current.get(c.pk)} for c in ExpenseCategory.objects.filter(is_active=True)],
        "trackers": Tracker.objects.all(), "kinds": Tracker.Kind.choices,
        "tokens": ApiToken.objects.filter(is_active=True).order_by("-created_at"),
        "api_url": request.build_absolute_uri(reverse("api:log")),
        "links": [(label, reverse(name)) for label, name in ADMIN_LINKS],
        "people": get_user_model().objects.filter(is_active=True).order_by("username")
        if request.user.is_superuser else [],
    })


@require_POST
def general(request):
    p, s = request.POST, AppSettings.load()
    try:
        zone = p.get("timezone", "").strip()
        try:
            ZoneInfo(zone)
        except (KeyError, ValueError):
            raise Invalid(f"“{zone}” is not a timezone. Use a name like America/Toronto.") from None
        s.timezone = zone
        s.day_start = _time(p.get("day_start"), "Day start")
        s.reminder_time = _time(p.get("reminder_time"), "Reminder time")
        s.weekly_review_time = _time(p.get("weekly_review_time"), "Review time")
        if p.get("currency") and p["currency"] != s.default_currency:
            if currency_locked():
                raise Invalid("The main currency can't change once you've logged spending.")
            if p["currency"] not in CURRENCIES:
                raise Invalid("Pick a currency from the list.")
            s.default_currency = p["currency"]
    except Invalid as e:
        return _error(e)
    s.reminder_enabled = p.get("reminder_enabled") == "on"
    s.weekly_review_enabled = p.get("weekly_review_enabled") == "on"
    s.save()
    return _saved("Settings saved. The day boundary applies to new entries only.")


def _target(metric: metrics.Metric, text: str) -> Decimal:
    text = (text or "").strip()
    if metric.fmt == "minutes":
        minutes = parse_duration(text)
        if minutes is None:
            raise Invalid("Enter a duration like 7h or 45m.")
        return Decimal(minutes)
    try:
        value = Decimal(text.replace("$", "").replace(",", ""))
    except InvalidOperation:
        raise Invalid("Enter a number for the target.") from None
    if value < 0 or not value.is_finite():
        raise Invalid("The target can't be negative.")
    return value


@require_POST
def goal_add(request):
    p = request.POST
    metric = next((m for m in _goal_metrics() if m.key == p.get("metric")), None)
    try:
        if metric is None:
            raise Invalid("Pick what the goal is about.")
        if p.get("comparator") not in Goal.Comparator.values or p.get("period") not in Goal.Period.values:
            raise Invalid("Pick “at most” or “at least”, and a period.")
        target = _target(metric, p.get("target"))
    except Invalid as e:
        return _error(e)
    word = "At most" if p["comparator"] == "lte" else "At least"
    per = {"day": "a day", "week": "a week", "month": "a month"}[p["period"]]
    name = p.get("name", "").strip() or f"{word} {fmt.value(metric.fmt, target)} {metric.name.lower()} {per}"
    Goal.objects.create(name=name[:64], metric=metric.key, comparator=p["comparator"], target=target,
                        period=p["period"], sort_order=Goal.objects.count())
    return _saved(f"Goal added: {name}")


@require_POST
def goal_change(request, pk, action):
    goal = get_object_or_404(Goal, pk=pk)
    if action == "delete":
        goal.delete()
        return _saved("Goal deleted.")
    if action == "toggle":
        goal.is_active = not goal.is_active
        goal.save(update_fields=["is_active"])
        return _saved("Goal resumed." if goal.is_active else "Goal paused.")
    return _error("Unknown action.")


@require_POST
def budgets(request):
    try:
        amounts = budget_amounts(request.POST, ExpenseCategory.objects.filter(is_active=True))
    except Invalid as e:
        return _error(e)
    save_budgets(amounts)
    return _saved("Budgets saved.")


def budget_amounts(post, categories) -> list:
    """[(category, amount or None)] from the `budget-<pk>` fields."""
    out = []
    for category in categories:
        text = post.get(f"budget-{category.pk}", "").strip().replace("$", "").replace(",", "")
        try:
            amount = Decimal(text) if text else None
        except InvalidOperation:
            raise Invalid(f"The budget for {category.name} isn't a number.") from None
        if amount is not None and (amount < 0 or not amount.is_finite()):
            raise Invalid(f"The budget for {category.name} can't be negative.")
        out.append((category, amount))
    return out


def save_budgets(amounts):
    """One amount per category. A change takes effect from this month and
    earlier months keep the budget they had."""
    month = days.today().replace(day=1)
    for category, amount in amounts:
        latest = Budget.objects.filter(category=category, effective_from__lte=month).order_by("-effective_from").first()
        if not amount:
            # Blank or 0 ends the budget from this month on. A zero row records
            # that, so earlier months keep the budget they had.
            if latest and latest.monthly_amount:
                Budget.objects.update_or_create(category=category, effective_from=month,
                                                defaults={"monthly_amount": Decimal(0)})
        elif latest is None or latest.monthly_amount != amount:
            Budget.objects.update_or_create(category=category, effective_from=month, defaults={"monthly_amount": amount})


@require_POST
def tracker_add(request):
    try:
        tracker = create_tracker(request.POST)
    except Invalid as e:
        return _error(e)
    return _saved(f"Tracker added: {tracker.name}. Log it with “{tracker.slug}” in the palette or /{tracker.slug} in Telegram.")


def create_tracker(p) -> Tracker:
    """From name, kind and optional slug, unit, aliases, min, max. Raises Invalid."""
    name, kind = p.get("name", "").strip(), p.get("kind")
    slug = slugify(p.get("slug", "").strip() or name)
    if not name or not slug:
        raise Invalid("Give the tracker a name.")
    if kind not in Tracker.Kind.values:
        raise Invalid("Pick a kind.")
    if Tracker.objects.filter(slug=slug).exists():
        raise Invalid(f"There is already a tracker called “{slug}”.")
    lo = hi = None
    if kind == Tracker.Kind.SCALE:
        try:
            lo, hi = Decimal(p.get("min") or "1"), Decimal(p.get("max") or "5")
        except InvalidOperation:
            raise Invalid("The scale's lowest and highest values must be numbers.") from None
        if lo >= hi:
            raise Invalid("The scale's highest value must be above its lowest.")
    elif kind == Tracker.Kind.DURATION:
        lo, hi = Decimal(0), Decimal(1440)
    return Tracker.objects.create(
        slug=slug, name=name, kind=kind, unit="min" if kind == Tracker.Kind.DURATION else p.get("unit", "").strip(),
        min_value=lo, max_value=hi, aliases=[a for a in p.get("aliases", "").split(",") if a.strip()],
        sort_order=Tracker.objects.count(),
    )


@require_POST
def tracker_toggle(request, pk):
    tracker = get_object_or_404(Tracker, pk=pk)
    tracker.is_active = not tracker.is_active
    tracker.save(update_fields=["is_active"])
    return _saved(f"{tracker.name} is active again." if tracker.is_active
                  else f"{tracker.name} is hidden. Its history is kept.")


@require_POST
def token_add(request):
    name = request.POST.get("name", "").strip()
    if not name:
        return _error("Name the token after where it will be used, e.g. iPhone Shortcuts.")
    token, raw = ApiToken.issue(name[:64])
    return render(request, "web/partials/new_token.html", {"token": token, "raw": raw})


@require_POST
def token_revoke(request, pk):
    get_object_or_404(ApiToken, pk=pk).delete()
    return _saved("Token revoked. Anything using it will stop working.")


@require_POST
@user_passes_test(lambda u: u.is_superuser)
def invite(request):
    try:
        user, created = invites.invite(request.POST.get("username", ""), invited_by=request.user)
    except invites.InviteError as e:
        return _error(e)
    return render(request, "web/partials/invite_link.html", {
        "invitee": user, "created": created, "link": request.build_absolute_uri(invites.path(user)),
        "days": django_settings.PASSWORD_RESET_TIMEOUT // 86400,
    })
