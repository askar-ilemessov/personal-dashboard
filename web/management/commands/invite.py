from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from web import invites


class Command(BaseCommand):
    help = "Create an account (or reset an existing one's password) and print a one-time link to set the password."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--base-url", help="e.g. https://dash.example.com (default: https://$DOMAIN)")

    def handle(self, *args, **opts):
        try:
            user, created = invites.invite(opts["username"])
        except invites.InviteError as e:
            raise CommandError(str(e)) from None
        base = opts["base_url"] or (f"https://{settings.DOMAIN}" if settings.DOMAIN else "http://localhost:8000")
        what = "Created" if created else "Password reset link for"
        self.stdout.write(f"{what} {user.username}. Send them this link (it works once):")
        self.stdout.write(base.rstrip("/") + invites.path(user))
