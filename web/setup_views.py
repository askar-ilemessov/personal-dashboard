"""Onboarding for invited accounts: set a password (Welcome), then five short
steps that edit the same configuration as Settings. Each step saves on
Continue, so leaving halfway resumes at the first unfinished step.

AppSettings.setup_step is the first unfinished step ("" when done). Accounts
not created by an invite start with it blank and never see these pages."""

from datetime import time

from django.contrib.auth import views as auth_views
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from tracking import days, defaults
from tracking.models import (
    ApiToken,
    AppSettings,
    Budget,
    ExpenseCategory,
    Goal,
    Tracker,
)

from . import invites
from .settings_views import (
    CURRENCIES,
    Invalid,
    budget_amounts,
    create_tracker,
    currency_locked,
    save_budgets,
    telegram_context,
)

STEPS = ["about", "track", "spending", "goals", "logging"]
TRACKER_GROUPS = [
    ("Habits", "done / not done", {Tracker.Kind.HABIT}),
    ("Counters", "add up through the day", {Tracker.Kind.COUNTER}),
    ("Time and ratings", "one value per day", {Tracker.Kind.DURATION, Tracker.Kind.SCALE}),
]


def examples() -> list[str]:
    """Quick-log examples that work for the acting user's own trackers."""
    trackers = list(Tracker.objects.filter(is_active=True))
    out = []
    if counter := next((t for t in trackers if t.kind == Tracker.Kind.COUNTER), None):
        out.append(f"2 {counter.slug}")
    if scale := next((t for t in trackers if t.kind == Tracker.Kind.SCALE), None):
        out.append(f"{scale.slug} {int(scale.max_value or 5) - 1}")
    if habit := next((t for t in trackers if t.kind == Tracker.Kind.HABIT), None):
        out.append(habit.slug)
    if category := ExpenseCategory.objects.filter(is_active=True).first():
        out.append(f"spent 12.50 {category.slug} lunch")
    return [*out, "woke 7:10", "gym legs 60"][:5]


# --- the steps ---------------------------------------------------------------


def _about(request, s):
    p = request.POST
    try:
        zone = days.zone_name(p.get("timezone", ""))
    except ValueError as e:
        raise Invalid(str(e)) from None
    try:
        day_start = time.fromisoformat(p.get("day_start", ""))
    except ValueError:
        raise Invalid("Day start must be a time like 04:00.") from None
    if not currency_locked():
        currency = p.get("currency", "")
        if currency not in CURRENCIES:
            raise Invalid("Pick a currency from the list.")
        s.default_currency = currency
    s.timezone, s.day_start = zone, day_start
    s.save()
    request.user.first_name = p.get("name", "").strip()[:150]
    request.user.save(update_fields=["first_name"])


def _track(request, s):
    keep = set(request.POST.getlist("tracker"))
    Tracker.objects.filter(slug__in=keep).update(is_active=True)
    Tracker.objects.exclude(slug__in=keep).update(is_active=False)


def _spending(request, s):
    if request.POST.get("spending") != "on":
        ExpenseCategory.objects.update(is_active=False)
        return
    keep = set(request.POST.getlist("category"))
    if not keep:
        raise Invalid("Tick at least one category, or untick “I want to track spending”.")
    amounts = budget_amounts(request.POST, ExpenseCategory.objects.filter(slug__in=keep))
    ExpenseCategory.objects.filter(slug__in=keep).update(is_active=True)
    ExpenseCategory.objects.exclude(slug__in=keep).update(is_active=False)
    save_budgets(amounts)


def _goals(request, s):
    offered = {g[0] for g in defaults.suggested_goals()}
    defaults.set_goals(offered & set(request.POST.getlist("goal")))


SAVE = {"about": _about, "track": _track, "spending": _spending, "goals": _goals, "logging": lambda request, s: None}


def _context(step, request, s) -> dict:
    if step == "about":
        return {"timezones": days.zone_choices(), "currencies": CURRENCIES, "locked": currency_locked(),
                "detect_timezone": s.setup_step == "about"}
    if step == "track":
        trackers = list(Tracker.objects.order_by("sort_order", "name"))
        return {"groups": [(label, note, [t for t in trackers if t.kind in kinds]) for label, note, kinds in TRACKER_GROUPS],
                "kinds": Tracker.Kind.choices}
    if step == "spending":
        month = days.today(s).replace(day=1)
        latest = {}
        for b in Budget.objects.filter(effective_from__lte=month).order_by("effective_from"):
            latest[b.category_id] = b.monthly_amount
        categories = list(ExpenseCategory.objects.order_by("sort_order", "name"))
        return {"categories": [{"category": c, "budget": latest.get(c.pk) or ""} for c in categories],
                "spending": any(c.is_active for c in categories)}
    if step == "goals":
        return {"goals": [
            {"key": key, "name": name, "ticked": Goal.objects.filter(
                metric=metric, comparator=comparator, period=period, is_active=True).exists()}
            for key, name, metric, comparator, _target, period, _, _ in defaults.suggested_goals()]}
    return {"examples": examples(), "has_token": ApiToken.objects.filter(is_active=True).exists(),
            **telegram_context()}


def _advance(s, step):
    """Move past `step`, unless the account is already further along
    (Back, then Continue again)."""
    i = STEPS.index(step)
    following = STEPS[i + 1] if i + 1 < len(STEPS) else ""
    if STEPS.index(s.setup_step) <= i:
        s.setup_step = following
        if not following:
            s.setup_deferred = False
        s.save(update_fields=["setup_step", "setup_deferred"])
    return redirect("web:setup", following) if following else redirect("web:today")


def step(request, name):
    s = AppSettings.load()
    if not s.setup_step:
        return redirect("web:today")
    if name not in STEPS:
        raise Http404
    error = ""
    if request.method == "POST":
        action = request.POST.get("action", "continue")
        if action == "later":
            s.setup_deferred = True
            s.save(update_fields=["setup_deferred"])
            return redirect("web:today")
        if action == "skip":
            return _advance(s, name)
        try:
            if action == "add" and name == "track":
                _track(request, s)  # keep the ticks changed so far
                tracker = create_tracker(request.POST)
                return redirect(reverse("web:setup", args=["track"]) + f"#tracker-{tracker.slug}")
            SAVE[name](request, s)
        except Invalid as e:
            error = str(e)
        else:
            return _advance(s, name)
    i = STEPS.index(name)
    return render(request, "web/setup.html", {
        "step": name, "number": i + 1, "total": len(STEPS), "settings": s, "error": error,
        "previous": STEPS[i - 1] if i else None, "last": i == len(STEPS) - 1,
        "bars": [n <= i for n in range(len(STEPS))], **_context(name, request, s),
    })


def start(request):
    s = AppSettings.load()
    return redirect("web:setup", s.setup_step) if s.setup_step else redirect("web:today")


class Welcome(auth_views.PasswordResetConfirmView):
    """The invite link: choose a password, then you're signed in and setup starts."""

    template_name = "registration/welcome.html"
    post_reset_login = True
    success_url = "/"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.user is not None:
            context["inviter"] = invites.inviter_name(self.user)
        return context


class SetupMiddleware:
    """Unfinished accounts are sent to their setup step, unless they chose
    “Skip setup”. Signing out, the API, the admin and static files are left alone."""

    EXEMPT = ("/setup/", "/settings/tokens/", "/settings/telegram/", "/logout/", "/login/", "/welcome/", "/password/", "/admin/", "/api/",
              "/static/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and not request.path.startswith(self.EXEMPT):
            s = AppSettings.load()
            if s.setup_step and not s.setup_deferred:
                return redirect("web:setup", s.setup_step)
        return self.get_response(request)
