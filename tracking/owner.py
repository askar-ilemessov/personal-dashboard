"""Whose data is being read or written. The only code that knows about it.

Every user-owned model's default manager is scoped to the acting user, so
`Tracker.objects.filter(...)` only ever sees that user's rows, and saving a
new row assigns it to them. With no acting user, queries raise instead of
returning everyone's data.

The acting user is set at the edges, never in business logic:
    web     OwnerMiddleware, from the logged-in user
    API     api.auth.BearerToken, from the token's owner
    bot     bot.handlers, the account TELEGRAM_ACCOUNT names
    jobs    bot.jobs.run_due, once per user

    with acting_as(user):
        logbook.apply([...], source="web")

Code that must see every user's rows (scheduled jobs, token lookup) uses
`Model.all_users`, the unscoped manager.
"""

from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.db import models

_acting = ContextVar("acting_user", default=None)


class NoActingUser(RuntimeError):
    """A user-owned table was queried with no acting user set."""


def current():
    user = _acting.get()
    if user is None:
        raise NoActingUser("No acting user. Wrap the call in tracking.owner.acting_as(user).")
    return user


def current_or_none():
    return _acting.get()


@contextmanager
def acting_as(user):
    token = _acting.set(user)
    try:
        yield user
    finally:
        _acting.reset(token)


def account_for_command(username: str | None):
    """The account a management command works on: the named one, or the
    only account if there is just one. Raises CommandError otherwise."""
    from django.contrib.auth import get_user_model
    from django.core.management.base import CommandError

    users = get_user_model().objects.filter(is_active=True)
    if username:
        user = users.filter(username=username).first()
        if user is None:
            raise CommandError(f"No active account called {username!r}.")
        return user
    if current_or_none():  # already acting as someone (tests)
        return current_or_none()
    if users.count() != 1:
        raise CommandError("Pass --user <username> to say which account to use.")
    return users.get()


class OwnedManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(user=current())


class OwnedModel(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, editable=False, related_name="+"
    )

    objects = OwnedManager()
    all_users = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.user_id is None:
            self.user = current()
        super().save(*args, **kwargs)

    def validate_constraints(self, exclude=None):
        # `user` is not editable, so forms leave it out of constraint checks;
        # put it back so a duplicate slug is a form error, not a crash.
        if self.user_id is None:
            self.user = current()
        super().validate_constraints(exclude=set(exclude or ()) - {"user"})


def act_for_rest_of_request(user):
    """For authentication that happens inside a view (the API's bearer
    token). OwnerMiddleware puts the previous value back afterwards."""
    _acting.set(user)


class OwnerMiddleware:
    """Act as the logged-in user for the rest of the request. Anonymous
    requests act as nobody, even if a reused worker thread did before."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        with acting_as(user if user is not None and user.is_authenticated else None):
            return self.get_response(request)
