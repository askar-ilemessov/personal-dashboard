from django.core.management.base import BaseCommand

from tracking import defaults


class Command(BaseCommand):
    help = "Create the default settings, trackers, categories, budgets and goals (idempotent)."

    def handle(self, *args, **options):
        defaults.install()
        self.stdout.write(self.style.SUCCESS("Defaults installed."))
