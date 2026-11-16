"""Onboarding for invited accounts (web/setup_views.py)."""

import pytest
from django.test import Client

from tracking import days, logbook, metrics
from tracking.logbook import ExpenseLog
from tracking.models import ApiToken, AppSettings, Budget, ExpenseCategory, Goal, Tracker
from tracking.owner import acting_as
from web import invites


@pytest.fixture
def friend(owner):
    user, _ = invites.invite("friend", invited_by=owner)
    return user


@pytest.fixture
def web(friend):
    client = Client()
    client.force_login(friend)
    return client


def mine(user, fn):
    with acting_as(user):
        return fn()


def active_trackers(user):
    return mine(user, lambda: set(Tracker.objects.filter(is_active=True).values_list("slug", flat=True)))


def goals(user):
    return mine(user, lambda: set(Goal.objects.values_list("metric", "period")))


def about(**changes):
    return {"name": "Dana", "timezone": "America/Vancouver", "day_start": "05:00", "currency": "USD"} | changes


def test_invited_accounts_start_small_and_remember_who_invited_them(owner, friend):
    assert active_trackers(friend) == {"coffee", "mood", "energy"}
    assert goals(friend) == {("tracker:coffee", "day"), ("sleep.duration", "day")}
    assert mine(friend, lambda: AppSettings.load().setup_step) == "about"
    assert invites.inviter_name(friend) == "me"
    assert len(active_trackers(owner)) == 10  # the inviter's own setup is untouched


def test_welcome_page_names_the_inviter(owner, friend):
    owner.first_name = "Askar"
    owner.save()
    page = Client().get(invites.path(friend), follow=True).content.decode()
    assert "Askar invited you" in page


def test_unfinished_accounts_are_sent_to_their_step(web):
    for path in ("/", "/expenses/", "/settings/"):
        response = web.get(path)
        assert response.status_code == 302 and response["Location"] == "/setup/about/", path
    assert web.get("/setup/").status_code == 302
    assert web.get("/setup/about/").status_code == 200
    assert web.get("/api/v1/today").status_code == 401  # the API answers for itself
    assert web.post("/logout/").status_code == 302


def test_accounts_that_were_not_invited_never_see_setup(owner):
    client = Client()
    client.force_login(owner)
    assert client.get("/").status_code == 200
    assert client.get("/setup/about/")["Location"] == "/"


def test_about_saves_the_basics(web, friend):
    response = web.post("/setup/about/", about())
    assert response["Location"] == "/setup/track/"
    friend.refresh_from_db()
    s = mine(friend, AppSettings.load)
    assert (friend.first_name, s.timezone, str(s.day_start), s.default_currency) == ("Dana", "America/Vancouver", "05:00:00", "USD")
    assert s.setup_step == "track"


@pytest.mark.parametrize("bad, message", [({"timezone": "Mars/Base"}, "not a timezone"),
                                          ({"day_start": "late"}, "Day start"), ({"currency": "XYZ"}, "currency")])
def test_about_shows_errors_on_the_page(web, friend, bad, message):
    response = web.post("/setup/about/", about(**bad))
    assert response.status_code == 200 and message in response.content.decode()
    assert mine(friend, AppSettings.load).setup_step == "about"


def test_currency_is_fixed_once_spending_is_logged(web, friend):
    with acting_as(friend):
        logbook.apply([ExpenseLog(amount="5", category="food")], source="web")
    web.post("/setup/about/", about(currency="EUR"))
    assert mine(friend, AppSettings.load).default_currency == "CAD"
    assert 'name="currency"' not in web.get("/setup/about/").content.decode()


def test_track_switches_trackers_and_adds_your_own(web, friend):
    web.post("/setup/about/", about())
    response = web.post("/setup/track/", {"action": "add", "tracker": ["mood", "read"], "name": "Water", "kind": "counter"})
    assert response["Location"] == "/setup/track/#tracker-water"
    assert active_trackers(friend) == {"mood", "read", "water"}  # the ticks were kept too
    page = web.post("/setup/track/", {"action": "add", "name": "Water", "kind": "counter"}).content.decode()
    assert "already a tracker" in page
    web.post("/setup/track/", {"tracker": ["water", "alcohol"]})
    assert active_trackers(friend) == {"water", "alcohol"}


def test_spending_categories_and_budgets(web, friend):
    web.post("/setup/about/", about())
    web.post("/setup/track/", {"tracker": ["coffee"]})
    with acting_as(friend):
        food, fun = ExpenseCategory.objects.get(slug="food"), ExpenseCategory.objects.get(slug="fun")
    web.post("/setup/spending/", {"spending": "on", "category": ["food", "fun"],
                                  f"budget-{food.pk}": "300", f"budget-{fun.pk}": ""})
    with acting_as(friend):
        assert set(ExpenseCategory.objects.filter(is_active=True).values_list("slug", flat=True)) == {"food", "fun"}
        assert [(b["category"].slug, b["budget"]) for b in metrics.budgets(days.today())] == [("food", 300)]
        assert not Budget.objects.filter(category=fun, monthly_amount__gt=0, effective_from__gte=days.today().replace(day=1)).exists()


def test_opting_out_of_spending_hides_it(web, friend):
    for step, data in (("about", about()), ("track", {"tracker": ["coffee"]})):
        web.post(f"/setup/{step}/", data)
    assert "at least one category" in web.post("/setup/spending/", {"spending": "on"}).content.decode()
    web.post("/setup/spending/", {})
    with acting_as(friend):
        assert not ExpenseCategory.objects.filter(is_active=True).exists()
        assert "expenses.total" not in [m.key for m in metrics.catalog()]


def test_goals_are_offered_only_for_trackers_you_kept(web, friend):
    web.post("/setup/about/", about())
    web.post("/setup/track/", {"tracker": ["alcohol"]})
    page = web.get("/setup/goals/").content.decode()
    assert "drinks a week" in page and "coffees" not in page
    web.post("/setup/spending/", {"action": "skip"})
    web.post("/setup/goals/", {"goal": ["alcohol", "gym", "coffee"]})  # coffee isn't offered any more
    assert goals(friend) == {("tracker:alcohol", "week"), ("exercise.sessions", "week")}


def test_finishing_lands_on_today_with_first_steps(web, friend):
    web.post("/setup/about/", about())
    for step in ("track", "spending", "goals"):
        assert web.post(f"/setup/{step}/", {"action": "skip"}).status_code == 302
    page = web.get("/setup/logging/").content.decode()
    assert "<code>2 coffee</code>" in page
    response = web.post("/setup/logging/", {"action": "continue"})
    assert response["Location"] == "/"
    assert mine(friend, AppSettings.load).setup_step == ""
    today = web.get("/").content.decode()
    assert "Welcome, Dana" in today and "Set up your account" in today


def test_first_steps_go_away_after_the_first_entry(web, friend):
    with acting_as(friend):
        s = AppSettings.load()
        s.setup_step = ""
        s.save()
        logbook.apply([logbook.TrackerLog(tracker="coffee")], source="web")
    assert "first-steps" not in web.get("/").content.decode()


def test_skipping_keeps_the_starter_setup(web, friend):
    web.post("/setup/about/", {"action": "skip"})
    assert active_trackers(friend) == {"coffee", "mood", "energy"}
    assert mine(friend, AppSettings.load).setup_step == "track"


def test_skip_setup_goes_to_today_and_offers_to_finish(web, friend):
    response = web.post("/setup/about/", {"action": "later"})
    assert response["Location"] == "/"
    page = web.get("/").content.decode()
    assert web.get("/").status_code == 200 and 'href="/setup/about/"' in page and "Finish setting up" in page


def test_leaving_halfway_resumes_at_the_next_step(web, friend):
    web.post("/setup/about/", about())
    web.post("/setup/track/", {"tracker": ["coffee"]})
    again = Client()
    again.force_login(friend)
    assert again.get("/")["Location"] == "/setup/spending/"
    assert again.get("/setup/about/").status_code == 200  # Back still works
    again.post("/setup/about/", about(name="Dee"))
    assert mine(friend, AppSettings.load).setup_step == "spending"  # going back doesn't rewind progress


def test_a_shortcuts_token_can_be_made_during_setup(web, friend):
    assert "Copy it now" in web.post("/settings/tokens/", {"name": "iPhone Shortcuts"}).content.decode()
    assert mine(friend, lambda: ApiToken.objects.count()) == 1


def test_setup_only_changes_your_own_account(web, owner, friend):
    web.post("/setup/about/", about())
    web.post("/setup/track/", {"tracker": []})
    web.post("/setup/spending/", {})
    assert len(active_trackers(owner)) == 10
    assert ExpenseCategory.objects.filter(is_active=True).count() == 9
    assert mine(owner, AppSettings.load).timezone == "America/Toronto"
