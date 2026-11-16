"""Generate realistic fake history through the real logging service.

The data has structure worth finding later: late screen time pushes bedtime
back, drinks delay the next wake-up, short sleep means more coffee and less
energy, workouts lift mood. It also contains gaps (unlogged days), explicit
zeros, a sick spell, a trip with USD spending, a run of short nights and one
spending spike.
"""

import random
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tracking import days, defaults, logbook
from tracking.logbook import DayNoteLog, DayTagLog, ExpenseLog, SleepLog, TrackerLog, WorkoutLog
from tracking.models import (
    AppSettings,
    DayNote,
    DayTag,
    Entry,
    ExchangeRate,
    Expense,
    ExpenseCategory,
    Operation,
    RecurringExpense,
    SleepSession,
    Tag,
    Workout,
)
from tracking.owner import account_for_command, acting_as

HABITS = {"vitamins": 0.85, "read": 0.6, "stretch": 0.5, "meditate": 0.45}
WORKOUT_PLAN = {0: "push", 1: "pull", 3: "legs", 5: "cardio"}  # weekday -> type
NOTES = [
    "Good focus in the morning, faded after lunch.",
    "Long walk by the water. Needed that.",
    "Too many meetings.",
    "Cooked a proper dinner for once.",
    "Felt scattered all day.",
    "Caught up with an old friend.",
    "Productive day, shipped the thing.",
    "Rainy and slow.",
]
RECURRING = [
    # name, amount, category, day of month
    ("Rent", "1650.00", "housing", 1),
    ("Spotify", "11.99", "subscriptions", 5),
    ("Internet", "75.00", "housing", 12),
    ("Gym membership", "49.00", "health", 20),
    ("iCloud", "3.99", "subscriptions", 27),
]


class Command(BaseCommand):
    help = "Seed N days of realistic fake data (default 90)."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Username to seed (default: the only account, if there is one)")
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--seed", type=int, default=42, help="Random seed, for reproducible data")
        parser.add_argument("--reset", action="store_true", help="Delete ALL of this user's logged data first")
        parser.add_argument("--force", action="store_true", help="Allow --reset when DEBUG is off")

    def handle(self, *args, **opts):
        with acting_as(account_for_command(opts["user"])):
            self._seed(opts)

    @transaction.atomic
    def _seed(self, opts):
        if opts["reset"]:
            if not django_settings.DEBUG and not opts["force"]:
                raise CommandError("Refusing to --reset with DEBUG off. Pass --force if you mean it.")
            for model in (Entry, Expense, SleepSession, Workout, DayNote, DayTag, Tag, Operation,
                          RecurringExpense, ExchangeRate):
                model.objects.all().delete()
        elif Entry.objects.exists() or Expense.objects.exists():
            raise CommandError("There is already logged data. Use --reset to replace it.")

        defaults.install()
        self.rng = random.Random(opts["seed"])
        self.settings = AppSettings.load()
        today = days.today(self.settings)
        n = opts["days"]
        first = today - timedelta(days=n)

        ExchangeRate.objects.get_or_create(currency="USD", defaults={"rate": Decimal("1.37")})
        for name, amount, category, dom in RECURRING:
            start = (first.replace(day=1) - timedelta(days=1)).replace(day=dom)
            while start < first:  # first charge inside the window
                start = logbook._next_occurrence(
                    RecurringExpense(start_date=start, frequency="monthly"), start
                )
            RecurringExpense.objects.create(
                name=name, amount=Decimal(amount), category=ExpenseCategory.objects.get(slug=category),
                start_date=start,
            )

        plan = self._plan(n)
        for i in range(n):
            intents = self._day(first + timedelta(days=i), plan, i)
            if intents:
                day = first + timedelta(days=i)
                logbook.apply(intents, source="seed", now=self._at(day, 22.5))
        self._today(today, plan, n)
        logbook.materialize_recurring(today)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {n} days for {self.settings.user}: {Entry.objects.count()} entries, {Expense.objects.count()} expenses, "
            f"{SleepSession.objects.count()} nights, {Workout.objects.count()} workouts."
        ))

    # --- helpers -----------------------------------------------------------

    def _at(self, day, hour: float) -> datetime:
        """`hour` hours after local midnight of `day` (may exceed 24)."""
        return datetime.combine(day, time(0), tzinfo=self.settings.tz) + timedelta(hours=hour)

    def _plan(self, n):
        """Decide the day-level story up front, because sleep on day i
        depends on the evening of day i-1."""
        rng = self.rng
        sick = set(range(n - 55, n - 52))
        travel = set(range(n - 34, n - 30))
        crunch = set(range(n - 9, n - 6))  # deadline: three short nights
        plan = []
        for i in range(n + 1):
            plan.append({
                "sick": i in sick, "travel": i in travel, "crunch": i in crunch,
                "skip": rng.random() < 0.04 and i not in sick | travel | crunch,
            })
        return plan

    def _evening(self, day, p):
        """Drinks, screen minutes and bedtime (hours after midnight) for `day`."""
        if "bed" in p:
            return p
        rng = self.rng
        party = day.weekday() in (4, 5)
        drinks = 0
        if not p["sick"]:
            roll = rng.random()
            drinks = (rng.choice([2, 3, 3, 4, 5]) if roll < 0.55 else 0) if party else (
                rng.choice([1, 1, 2]) if roll < 0.15 else 0)
        screen = rng.gauss(190, 45) + (45 if day.weekday() >= 5 else 0) + (90 if p["sick"] else 0)
        screen = max(40, min(600, screen)) - (60 if p["travel"] else 0)
        bed = 23.9 + (screen - 190) / 60 * 0.3 + drinks * 0.22 + (0.6 if party else 0) + rng.gauss(0, 0.35)
        if p["crunch"]:
            bed += 2.3
        if p["sick"]:
            bed -= 1.2
        p.update(drinks=drinks, screen=int(max(30, screen)), bed=min(27.5, max(21.0, bed)))
        return p

    def _wake(self, day, p, prev):
        rng = self.rng
        wake = (8.6 if day.weekday() >= 5 else 7.1) + rng.gauss(0, 0.3) + prev["drinks"] * 0.27
        if p["sick"]:
            wake += 1.0
        if p["crunch"] or prev["crunch"]:
            wake = min(wake, 6.9 + rng.gauss(0, 0.15))
        return wake

    def _day(self, day, plan, i):
        rng = self.rng
        p = self._evening(day, plan[i])
        prev = self._evening(day - timedelta(days=1), plan[i - 1]) if i else dict(p, bed=23.5, drinks=0)
        if p["skip"]:
            return []
        out = []
        add = out.append

        # Sleep: last night's bedtime to this morning's wake.
        wake = self._wake(day, p, prev)
        sleep_h = wake + 24 - prev["bed"]
        if rng.random() > 0.04:
            add(SleepLog(bedtime=self._at(day - timedelta(days=1), prev["bed"]), wake_time=self._at(day, wake)))

        # Exercise
        workout = WORKOUT_PLAN.get(day.weekday())
        did_workout = bool(workout) and not p["sick"] and not p["travel"] and rng.random() < 0.82
        if did_workout:
            minutes = rng.randint(25, 45) if workout == "cardio" else rng.randint(45, 75)
            add(WorkoutLog(type=workout, duration_min=minutes, at=self._at(day, 18 + rng.random())))
        elif p["travel"] and rng.random() < 0.3:
            add(WorkoutLog(type="other", duration_min=rng.randint(30, 90), notes="Hike", at=self._at(day, 11)))

        # Coffee: more after a short night. Each cup is its own entry.
        cups = max(0, min(5, round(1.3 + max(0, 7 - sleep_h) * 0.8 + rng.gauss(0, 0.7))))
        for c in range(cups):
            add(TrackerLog(tracker="coffee", at=self._at(day, wake + 0.5 + c * 2.8 + rng.random())))
        if cups == 0:
            add(TrackerLog(tracker="coffee", value=0, mode="set", day=day))

        # Alcohol and junk: zeros are logged explicitly most of the time, otherwise left unlogged.
        if p["drinks"]:
            for d in range(p["drinks"]):
                add(TrackerLog(tracker="alcohol", at=self._at(day, 19.5 + d * 0.8)))
        elif rng.random() < 0.7:
            add(TrackerLog(tracker="alcohol", value=0, mode="set", day=day))
        junk = (rng.random() < 0.3) + (rng.random() < (0.5 if p["drinks"] >= 3 or sleep_h < 6 else 0.1))
        if junk:
            add(TrackerLog(tracker="junk", value=junk, at=self._at(day, 15 + rng.random() * 6)))
        elif rng.random() < 0.6:
            add(TrackerLog(tracker="junk", value=0, mode="set", day=day))

        if rng.random() > 0.08:
            add(TrackerLog(tracker="screen", value=p["screen"], at=self._at(day, 22.4)))

        # Mood and energy
        if rng.random() > 0.06:
            energy = 3.1 + (sleep_h - 7) * 0.6 - (1.3 if p["sick"] else 0) + rng.gauss(0, 0.55)
            mood = (3.2 + (0.6 if did_workout else 0) + (sleep_h - 7) * 0.3 - (0.4 if prev["drinks"] >= 3 else 0)
                    - (1.1 if p["sick"] else 0) + (0.5 if p["travel"] else 0) + rng.gauss(0, 0.6))
            add(TrackerLog(tracker="energy", value=max(1, min(5, round(energy))), at=self._at(day, 21.5)))
            add(TrackerLog(tracker="mood", value=max(1, min(5, round(mood))), at=self._at(day, 21.5)))

        # Habits
        for slug, chance in HABITS.items():
            if p["sick"] or p["travel"]:
                chance *= 0.4
            if rng.random() < chance:
                add(TrackerLog(tracker=slug, value=1, at=self._at(day, 8 + rng.random() * 13)))
            elif rng.random() < 0.6:
                add(TrackerLog(tracker=slug, value=0, at=self._at(day, 21.6)))

        # Context
        tags = [t for t in ("sick", "travel") if p[t]] + (["deadline"] if p["crunch"] else [])
        if tags:
            add(DayTagLog(tags=tags, day=day))
        if rng.random() < 0.15:
            add(DayNoteLog(note=rng.choice(NOTES), day=day))

        out.extend(self._expenses(day, p, i, len(plan) - 1))
        return out

    def _expenses(self, day, p, i, n):
        rng = self.rng
        money = lambda lo, hi: Decimal(f"{rng.uniform(lo, hi):.2f}")
        cur = "USD" if p["travel"] else None
        out = []

        def spend(lo, hi, category, note="", hour=13.0):
            out.append(ExpenseLog(amount=money(lo, hi), category=category, note=note, currency=cur,
                                  at=self._at(day, hour + rng.random() * 2)))

        if p["sick"]:
            if rng.random() < 0.5:
                spend(12, 40, "health", "Pharmacy")
            return out
        if day.weekday() in (2, 6) and not p["travel"]:
            spend(40, 115, "food", rng.choice(["Loblaws", "No Frills", "Farm Boy"]), 17)
        if rng.random() < (0.9 if p["travel"] else 0.6 if day.weekday() in (4, 5) else 0.28):
            spend(14, 62, "eating-out", rng.choice(["Lunch", "Dinner out", "Takeout", "Ramen", "Burrito"]), 12.5)
        if day.weekday() < 5 and rng.random() < 0.3 or p["travel"]:
            spend(3.3, 28, "transport", rng.choice(["Transit", "Uber", "Parking"]), 8.5)
        if p["drinks"] >= 2:
            spend(18, 75, "fun", "Bar", 20.5)
        elif rng.random() < 0.08:
            spend(12, 60, "fun", rng.choice(["Movie", "Concert ticket", "Game"]), 19)
        if rng.random() < 0.07:
            spend(20, 160, "shopping", rng.choice(["Clothes", "Amazon", "Books"]))
        if i == n - 21:
            out.append(ExpenseLog(amount=Decimal("648.99"), category="shopping", note="New monitor",
                                  at=self._at(day, 14.2)))
        return out

    def _today(self, today, plan, n):
        """Today is only partly logged, so the Today page has something to nag about."""
        now = datetime.now(self.settings.tz)
        prev = self._evening(today - timedelta(days=1), plan[n - 1])
        wake = self._at(today, self._wake(today, plan[n], prev))
        if wake >= now:
            return
        intents = [SleepLog(bedtime=self._at(today - timedelta(days=1), prev["bed"]), wake_time=wake)]
        if wake + timedelta(minutes=30) < now:
            intents.append(TrackerLog(tracker="coffee", at=wake + timedelta(minutes=30)))
        logbook.apply(intents, source="seed")
