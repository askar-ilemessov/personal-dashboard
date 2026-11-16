"""Invite-only accounts. An invite is a one-time link where the person picks
their own password (Django's password-reset token: it stops working once a
password is set, or after PASSWORD_RESET_TIMEOUT). The same link works as an
admin-driven password reset for an existing account."""

import re

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from tracking import defaults
from tracking.models import AppSettings
from tracking.owner import acting_as


class InviteError(ValueError):
    """The message is shown to the person inviting."""


def invite(username: str, invited_by=None):
    """(user, created). A new account has no usable password until the link
    is used, starts with the smaller starter setup, and goes through
    onboarding (web/setup_views.py) on first sign-in."""
    username = (username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", username):
        raise InviteError("Use a username of letters, digits, dots, - or _ only.")
    User = get_user_model()
    user = User.objects.filter(username__iexact=username).first()
    if user is not None:
        if not user.is_active:
            raise InviteError(f"{user.username} is deactivated. Reactivate them in the admin first.")
        return user, False
    user = User(username=username)
    user.set_unusable_password()
    user.save()  # tracking.apps installs their defaults
    with acting_as(user):
        defaults.start_invited()
        settings = AppSettings.load()
        settings.setup_step = "about"
        settings.invited_by = invited_by
        settings.save()
    return user, True


def inviter_name(user) -> str:
    """Who invited `user`, for the welcome page, or ""."""
    settings = AppSettings.all_users.filter(user=user).select_related("invited_by").first()
    by = settings.invited_by if settings else None
    return (by.first_name or by.username) if by else ""


def path(user) -> str:
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return reverse("welcome", args=[uid, default_token_generator.make_token(user)])
