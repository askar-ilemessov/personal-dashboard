"""Accounts never see or change each other's data, whichever way in."""

import json
from datetime import date
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client

from bot import jobs
from bot.tests.test_bot import Chatter, link
from insights import review
from insights.models import WeeklyReview
from parsing import parser
from parsing.models import Draft
from tracking import days, logbook, metrics
from tracking.logbook import ExpenseLog, LogError, TrackerLog
from tracking.models import (
    ApiToken,
    AppSettings,
    Entry,
    Expense,
    ExpenseCategory,
    Operation,
    RecurringExpense,
    TelegramLink,
    Tracker,
)
from tracking.owner import NoActingUser, acting_as, current_or_none


@pytest.fixture
def other(django_user_model):
    """A second account, set up the way a real invite would be."""
    user = django_user_model.objects.create_user("other", password="x")
    with acting_as(user):
        s = AppSettings.load()
        s.timezone = "America/Toronto"
        s.save()
    return user


def test_queries_without_an_acting_user_fail_instead_of_seeing_everyone():
    with acting_as(None), pytest.raises(NoActingUser):
        list(Tracker.objects.all())


def test_new_accounts_get_their_own_defaults(owner, other):
    with acting_as(other):
        assert Tracker.objects.filter(slug="coffee").count() == 1
        coffee = Tracker.objects.get(slug="coffee")
    assert Tracker.objects.get(slug="coffee") != coffee  # same slug, separate rows
    assert Tracker.all_users.filter(slug="coffee").count() == 2


def test_logging_and_reading_are_per_account(owner, other):
    today = days.today()
    logbook.apply([TrackerLog(tracker="coffee", value=3), ExpenseLog(amount="12", category="food")], source="web")
    with acting_as(other):
        assert metrics.value("tracker:coffee", today) is None
        assert metrics.value("expenses.total", today) == 0
        assert "tracker:coffee" in metrics.unlogged(today)
        assert metrics.first_day() is None
        logbook.apply([TrackerLog(tracker="coffee", value=1)], source="web")
        assert metrics.value("tracker:coffee", today) == 1
    assert metrics.value("tracker:coffee", today) == 3
    assert Entry.all_users.count() == 2 and Entry.objects.count() == 1


def test_a_tracker_added_by_one_account_is_unknown_to_another(owner, other):
    Tracker.objects.create(slug="water", name="Water", kind="counter")
    logbook.apply([TrackerLog(tracker="water")], source="web")
    with acting_as(other), pytest.raises(LogError, match="Unknown tracker"):
        logbook.apply([TrackerLog(tracker="water")], source="web")
    with acting_as(other):
        assert not parser.interpret("water", use_llm=False).ok


def test_one_account_cannot_undo_anothers_operation(owner, other):
    op = logbook.apply([TrackerLog(tracker="coffee")], source="web")
    with acting_as(other), pytest.raises(Operation.DoesNotExist):
        logbook.undo(Operation.objects.get(pk=op.pk))
    assert Entry.objects.count() == 1


def test_recurring_rules_and_reviews_are_per_account(owner, other):
    RecurringExpense.objects.create(name="Rent", amount=Decimal(1650), start_date=date(2026, 11, 1),
                                    category=ExpenseCategory.objects.get(slug="housing"))
    logbook.materialize_recurring(date(2026, 11, 14))
    with acting_as(other):
        assert logbook.materialize_recurring(date(2026, 11, 14)) is None
        assert not Expense.objects.exists()
        review.generate(date(2026, 11, 2), today=date(2026, 11, 9))
    review.generate(date(2026, 11, 2), today=date(2026, 11, 9))
    assert WeeklyReview.all_users.filter(week_start=date(2026, 11, 2)).count() == 2


def test_scheduled_jobs_materialise_every_accounts_rules(owner, other):
    for user in (owner, other):
        with acting_as(user):
            RecurringExpense.objects.create(name="Gym", amount=Decimal(49), start_date=date(2026, 11, 1),
                                            category=ExpenseCategory.objects.get(slug="health"))
    with acting_as(None):
        jobs.run_due()
    assert {e.user for e in Expense.all_users.all()} == {owner, other}


# --- web -------------------------------------------------------------------


@pytest.fixture
def web(owner):
    client = Client()
    client.force_login(owner)
    return client


@pytest.fixture
def theirs(other):
    """Rows the other account logged, which `web` must not reach."""
    with acting_as(other):
        op = logbook.apply([ExpenseLog(amount="99", category="food", note="their secret lunch")], source="web")
        logbook.apply([logbook.DayNoteLog(note="their private note")], source="web")
        expense = Expense.objects.get()
        draft = parser.save_draft(parser.interpret("coffee", use_llm=False), "web")
    return {"op": op, "expense": expense, "draft": draft}


def test_pages_show_only_your_own_rows(web, theirs):
    for path in ("/", "/expenses/", f"/day/{theirs['expense'].day}/", "/export/expense.csv"):
        body = web.get(path).content.decode()
        assert "their secret lunch" not in body and "their private note" not in body, path


def test_another_accounts_rows_are_not_found(web, theirs):
    pk = theirs["expense"].pk
    assert web.get(f"/row/expense/{pk}/").status_code == 404
    assert web.post(f"/row/expense/{pk}/edit/", {"amount": "1"}).status_code == 404
    assert web.post(f"/row/expense/{pk}/delete/").status_code == 404
    assert web.post(f"/undo/{theirs['op'].pk}/").status_code == 404
    assert Expense.all_users.get(pk=pk).amount == 99


def test_another_accounts_draft_is_not_confirmed(web, theirs):
    response = web.post("/palette/confirm/", {"text": "coffee", "draft": theirs["draft"].pk})
    assert json.loads(response["HX-Trigger"])["toast"]["message"].startswith("Coffee")  # re-parsed as your own
    assert Draft.all_users.filter(pk=theirs["draft"].pk).exists()
    assert Entry.all_users.filter(user=theirs["expense"].user).count() == 0


def test_a_request_leaves_no_acting_user_behind(web, owner, other):
    with acting_as(other):
        _, raw = ApiToken.issue("phone")
    with acting_as(None):
        Client(headers={"Authorization": f"Bearer {raw}"}).post(
            "/api/v1/log", {"text": "coffee"}, content_type="application/json")
        assert current_or_none() is None
        assert Client().get("/").status_code == 302  # anonymous: still sent to the login page


# --- API and bot -----------------------------------------------------------


def test_api_tokens_log_to_their_own_account(web, owner, other):
    with acting_as(other):
        _, raw = ApiToken.issue("phone")
    api = Client(headers={"Authorization": f"Bearer {raw}"})
    api.force_login(owner)  # a browser session must not change whose data it is
    assert api.post("/api/v1/log", {"text": "coffee"}, content_type="application/json").status_code == 201
    assert not Entry.objects.exists()
    with acting_as(other):
        assert Entry.objects.count() == 1
        op = Operation.objects.get()
    assert api.post("/api/v1/undo", {"operation": op.pk}, content_type="application/json").status_code == 200


def test_api_tokens_of_deactivated_accounts_stop_working(other):
    with acting_as(other):
        _, raw = ApiToken.issue("phone")
    other.is_active = False
    other.save()
    response = Client(headers={"Authorization": f"Bearer {raw}"}).get("/api/v1/today")
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
async def test_each_telegram_user_logs_to_the_account_they_linked(owner, other):
    from asgiref.sync import sync_to_async

    await sync_to_async(link)(owner, 1)
    await sync_to_async(link)(other, 2)
    chat = Chatter()
    with acting_as(other):  # whoever happens to be acting, the sender decides
        await chat.say("/coffee", user=1)
    await chat.say("/coffee 2", user=2)
    entries = sync_to_async(lambda u: [e.value for e in Entry.all_users.filter(user=u)])
    assert await entries(owner) == [1] and await entries(other) == [2]


def test_settings_only_shows_and_changes_your_own_telegram_link(web, other, settings):
    settings.TELEGRAM_BOT_USERNAME = "dash_bot"
    link(other, 2)
    assert b"Connect Telegram" in web.get("/settings/").content
    assert web.get("/settings/telegram/").status_code == 204  # owner has not linked
    web.post("/settings/telegram/disconnect/")
    assert TelegramLink.for_telegram(2).user == other


def test_scheduled_messages_go_to_each_linked_account(owner, other, monkeypatch):
    from bot import replies

    link(other, 2)
    monkeypatch.setattr(jobs, "evening_reminder", lambda now=None: replies.Reply(current_or_none().username))
    monkeypatch.setattr(jobs, "weekly_review", lambda now=None: None)
    with acting_as(None):
        assert [(chat, r.text) for chat, r in jobs.run_due()] == [(2, "other")]  # owner has no Telegram


# --- accounts --------------------------------------------------------------


def test_invites_need_a_superuser(web, django_user_model):
    assert web.post("/settings/invite/", {"username": "friend"}).status_code == 302
    assert not django_user_model.objects.filter(username="friend").exists()


def test_invite_link_sets_a_password_once(owner, django_user_model):
    owner.is_superuser = True
    owner.save()
    admin = Client()
    admin.force_login(owner)
    body = admin.post("/settings/invite/", {"username": "friend"}).content.decode()
    link = body.split('value="', 1)[1].split('"', 1)[0].removeprefix("http://testserver")
    friend = django_user_model.objects.get(username="friend")
    assert not friend.has_usable_password()

    guest = Client()
    form = guest.get(link, follow=True)  # Django swaps the token for a session-bound URL
    assert "Welcome, friend" in form.content.decode()
    response = guest.post(form.redirect_chain[-1][0], {"new_password1": "a long passphrase 42",
                                                       "new_password2": "a long passphrase 42"})
    assert response.status_code == 302 and response["Location"] == "/"
    assert guest.get("/")["Location"] == "/setup/about/"  # signed in, and setup starts
    with acting_as(friend):
        assert Tracker.objects.filter(slug="coffee").exists()
    assert "expired" in Client().get(link, follow=True).content.decode()


def test_seed_needs_to_know_which_account(other):
    with acting_as(None):
        with pytest.raises(Exception, match="--user"):
            call_command("seed", days=3, verbosity=0)
        call_command("seed", days=3, user="other", verbosity=0)
    assert not Entry.objects.exists()
    with acting_as(other):
        assert Entry.objects.exists()
