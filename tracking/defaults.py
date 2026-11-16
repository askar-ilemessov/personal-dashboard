"""Starting configuration. Everything here is ordinary data: edit, add or
deactivate it later in Settings or the admin without touching code."""

from datetime import date
from decimal import Decimal

from .models import AppSettings, Budget, ExpenseCategory, Goal, Tracker

TRACKERS = [
    # slug, name, kind, unit, min, max, aliases
    ("read", "Read 20 min", "habit", "", None, None, ["reading"]),
    ("meditate", "Meditate", "habit", "", None, None, ["meditation"]),
    ("vitamins", "Vitamins", "habit", "", None, None, []),
    ("stretch", "Stretch", "habit", "", None, None, ["stretching"]),
    ("coffee", "Coffee", "counter", "cups", None, None, ["coffees", "espresso"]),
    ("alcohol", "Alcohol", "counter", "drinks", None, None, ["drink", "drinks", "beer", "wine"]),
    ("junk", "Junk food", "counter", "items", None, None, ["junk food", "junkfood"]),
    ("screen", "Screen time", "duration", "min", 0, 1440, ["screen time", "screentime"]),
    ("mood", "Mood", "scale", "", 1, 5, []),
    ("energy", "Energy", "scale", "", 1, 5, []),
]

CATEGORIES = [
    # slug, name, aliases, monthly budget
    ("food", "Groceries", ["groceries", "grocery"], 500),
    ("eating-out", "Eating out", ["restaurant", "restaurants", "dining", "takeout", "lunch", "dinner"], 250),
    ("transport", "Transport", ["transit", "gas", "uber", "taxi"], 150),
    ("housing", "Housing", ["rent", "utilities"], None),
    ("subscriptions", "Subscriptions", ["subscription", "subs"], 60),
    ("health", "Health", ["pharmacy", "gym"], None),
    ("shopping", "Shopping", ["clothes", "amazon"], 200),
    ("fun", "Entertainment", ["entertainment", "bar", "movies", "games"], 150),
    ("other", "Other", ["misc"], None),
]

GOALS = [
    # name, metric, comparator, target, period
    ("At most 2 coffees", "tracker:coffee", "lte", 2, "day"),
    ("Sleep 7h+", "sleep.duration", "gte", 420, "day"),
    ("Gym 4× a week", "exercise.sessions", "gte", 4, "week"),
]


# Invited accounts start smaller: these trackers on, the rest offered but off,
# and the goals from SUGGESTED_GOALS marked as starters.
STARTER_TRACKERS = ["coffee", "mood", "energy"]

SUGGESTED_GOALS = [
    # key, name, metric, comparator, target, period, needs tracker, starter
    ("coffee", "At most 2 coffees a day", "tracker:coffee", "lte", 2, "day", "coffee", True),
    ("sleep", "Sleep 7h or more", "sleep.duration", "gte", 420, "day", None, True),
    ("gym", "Work out 3 times a week", "exercise.sessions", "gte", 3, "week", None, False),
    ("alcohol", "At most 7 drinks a week", "tracker:alcohol", "lte", 7, "week", "alcohol", False),
    ("screen", "Screen time under 3h a day", "tracker:screen", "lte", 180, "day", "screen", False),
]


def suggested_goals():
    """Suggestions whose tracker the acting user has switched on."""
    active = set(Tracker.objects.filter(is_active=True).values_list("slug", flat=True))
    return [g for g in SUGGESTED_GOALS if g[6] is None or g[6] in active]


def set_goals(keys):
    """Make the acting user's suggested goals match `keys`: ticked ones
    exist and are active, unticked ones are removed. Other goals are kept."""
    for key, name, metric, comparator, target, period, _, _ in SUGGESTED_GOALS:
        matching = Goal.objects.filter(metric=metric, comparator=comparator, period=period)
        if key in keys:
            if not matching.update(is_active=True):
                Goal.objects.create(name=name, metric=metric, comparator=comparator, target=target, period=period,
                                    sort_order=Goal.objects.count())
        else:
            matching.delete()


def start_invited():
    """Trim the acting user's fresh defaults to the starter set."""
    Tracker.objects.exclude(slug__in=STARTER_TRACKERS).update(is_active=False)
    Goal.objects.all().delete()
    set_goals({g[0] for g in SUGGESTED_GOALS if g[7]})


def install():
    """For the acting user (see tracking.owner). Idempotent: creates whatever
    is missing, never overwrites edits. New accounts get this automatically."""
    AppSettings.load()
    for i, (slug, name, kind, unit, lo, hi, aliases) in enumerate(TRACKERS):
        Tracker.objects.get_or_create(
            slug=slug,
            defaults=dict(
                name=name, kind=kind, unit=unit, min_value=lo, max_value=hi, aliases=aliases, sort_order=i
            ),
        )
    for i, (slug, name, aliases, budget) in enumerate(CATEGORIES):
        category, created = ExpenseCategory.objects.get_or_create(
            slug=slug, defaults=dict(name=name, aliases=aliases, sort_order=i)
        )
        if created and budget:
            Budget.objects.create(
                category=category, monthly_amount=Decimal(budget), effective_from=date(2000, 1, 1)
            )
    if not Goal.objects.exists():
        for i, (name, metric, comparator, target, period) in enumerate(GOALS):
            Goal.objects.create(
                name=name, metric=metric, comparator=comparator, target=target, period=period, sort_order=i
            )
