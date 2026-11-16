from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from tracking import defaults
from tracking.owner import acting_as


class Command(BaseCommand):
    help = "Create the default settings, trackers, categories, budgets and goals for every account (idempotent)."

    def handle(self, *args, **options):
        users = get_user_model().objects.filter(is_active=True)
        for user in users:
            with acting_as(user):
                defaults.install()
        self.stdout.write(self.style.SUCCESS(f"Defaults installed for {users.count()} account(s)."))
