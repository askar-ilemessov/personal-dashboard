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


def install():
    """Idempotent: creates whatever is missing, never overwrites edits."""
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
