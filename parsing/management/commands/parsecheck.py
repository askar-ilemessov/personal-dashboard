"""Try the shared parser from the terminal. Nothing is saved.

    manage.py parsecheck "had two coffees and a beer with dinner"
    manage.py parsecheck --eval      # run the built-in sentences through Claude

--eval calls the real API (a few cents on the default model) and is the
way to check the prompt after changing it or the model.
"""

from django.core.management.base import BaseCommand, CommandError

from parsing import parser
from tracking.owner import account_for_command, acting_as

# (sentence, text that must appear in the preview lines)
CASES = [
    ("had two coffees this morning and a beer with dinner", ["Coffee +2", "Alcohol +1"]),
    ("no alcohol today", ["Alcohol = 0"]),
    ("bought lunch for 14.50", ["$14.50", "Eating out"]),
    ("grabbed groceries, forty two dollars", ["$42.00", "Groceries"]),
    ("went to bed at half past eleven and got up at ten past seven", ["Slept 23:30 → 07:10"]),
    ("did legs at the gym for about an hour", ["Workout: Legs", "1h"]),
    ("went for a 35 minute run", ["Workout: Cardio", "35m"]),
    ("feeling great today, energy is okay", ["Mood: 5", "Energy: 3"]),
    ("did my reading but skipped meditation", ["Read 20 min: done", "Meditate: not done"]),
    ("about three and a half hours of screen time", ["Screen time: 3h 30m"]),
    ("I'm sick today", ["#sick"]),
    ("yesterday I had three coffees", ["Coffee +3", "("]),
    ("how many coffees did I have last week?", []),  # a question: nothing to log
]


class Command(BaseCommand):
    help = "Parse text with the shared parser and print the preview. Saves nothing."

    def add_arguments(self, parser_):
        parser_.add_argument("text", nargs="?")
        parser_.add_argument("--eval", action="store_true", help="Run the built-in sentences through Claude")
        parser_.add_argument("--user", help="Whose trackers and categories to use (default: the only account)")

    def handle(self, *args, **options):
        with acting_as(account_for_command(options["user"])):
            self.run(options)

    def run(self, options):
        if options["eval"]:
            return self.evaluate()
        if not options["text"]:
            raise CommandError("Give some text, or --eval.")
        self.show(parser.interpret(options["text"]))

    def show(self, result):
        self.stdout.write(f"tier: {result.tier}")
        for line in result.lines if result.intents else []:
            self.stdout.write(f"  + {line}")
        if result.ignored:
            self.stdout.write(f"  not logged: {result.ignored}")
        for error in result.errors:
            self.stdout.write(self.style.ERROR(f"  ! {error}"))

    def evaluate(self):
        if not parser.llm_enabled():
            raise CommandError("Set ANTHROPIC_API_KEY in .env first.")
        passed = 0
        for text, expected in CASES:
            result = parser.interpret(text)
            joined = "\n".join(result.lines) if result.intents else ""
            ok = (all(e in joined for e in expected) and not result.errors) if expected else not result.ok
            passed += ok
            self.stdout.write((self.style.SUCCESS("PASS ") if ok else self.style.ERROR("FAIL ")) + text)
            if not ok:
                self.show(result)
        self.stdout.write(f"\n{passed} of {len(CASES)} passed")
