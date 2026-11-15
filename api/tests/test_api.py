import pytest
from django.test import Client

from tracking import days, metrics
from tracking.models import ApiToken, Entry, Operation, SleepSession


@pytest.fixture
def token():
    return ApiToken.issue("iPhone Shortcuts")


@pytest.fixture
def api(token):
    return Client(headers={"Authorization": f"Bearer {token[1]}"})


def post(client, path, data):
    return client.post(path, data, content_type="application/json")


def test_only_the_hash_is_stored(token):
    obj, raw = token
    assert len(raw) >= 40 and raw not in str(obj.__dict__) and obj.token_hash == ApiToken.digest(raw)
    assert ApiToken.lookup(raw) == obj and ApiToken.lookup("nope") is None


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Bearer"},
                                     {"Authorization": "Basic abc"}, {"Authorization": "Bearer a b"}])
def test_requests_without_a_valid_token_are_refused(token, headers):
    client = Client(headers=headers)
    for response in (post(client, "/api/v1/log", {"text": "coffee"}), client.get("/api/v1/today")):
        assert response.status_code == 401 and response["WWW-Authenticate"] == "Bearer"
        assert "/login/" not in response.get("Location", "")  # an API error, not a redirect to the login page
    assert not Entry.objects.exists()


def test_a_browser_session_is_not_enough(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("me"))
    assert post(client, "/api/v1/log", {"text": "coffee"}).status_code == 401


def test_revoked_tokens_stop_working(api, token):
    assert post(api, "/api/v1/log", {"text": "coffee"}).status_code == 201
    token[0].delete()
    assert post(api, "/api/v1/log", {"text": "coffee"}).status_code == 401
    assert Entry.objects.count() == 1


def test_log_woke_like_an_alarm_shortcut_would(api, token):
    assert post(api, "/api/v1/log", {"text": "slept 23:30"}).status_code == 201
    response = post(api, "/api/v1/log", {"text": "woke"})
    body = response.json()
    assert response.status_code == 201 and body["ok"] and body["logged"][0].startswith("Woke up")
    session = SleepSession.objects.get()
    assert session.wake_time and session.source == "shortcut"
    assert Operation.objects.get(pk=body["operation"]).raw_text == "woke"
    token[0].refresh_from_db()
    assert token[0].last_used_at is not None


def test_log_several_things_form_encoded_too(api):
    response = api.post("/api/v1/log", {"text": "2 coffees, mood 4"})  # form body, as some Shortcuts send
    assert response.json()["message"] == "Coffee +2; Mood: 4"
    assert metrics.value("tracker:coffee", days.today()) == 2


@pytest.mark.parametrize("data,fragment", [({"text": "blargh"}, "understand"), ({"text": "mood 9"}, "at most 5"),
                                           ({"text": ""}, "Send JSON"), ({}, "Send JSON")])
def test_bad_requests_explain_themselves_and_save_nothing(api, data, fragment):
    response = post(api, "/api/v1/log", data)
    assert response.status_code == 400 and not response.json()["ok"] and fragment in response.json()["errors"][0]
    assert not Entry.objects.exists()


def test_claude_is_only_used_when_asked(api, settings, monkeypatch):
    from parsing import llm
    from parsing.tests.fakes import FakeClaude, item

    fake = FakeClaude([item("tracker", tracker="coffee", value=1)])
    settings.ANTHROPIC_API_KEY = "test-key"
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda **kwargs: fake)
    assert post(api, "/api/v1/log", {"text": "had a flat white"}).status_code == 400 and fake.requests == []
    assert post(api, "/api/v1/log", {"text": "had a flat white", "llm": True}).json()["logged"] == ["Coffee +1"]


def test_undo(api):
    operation = post(api, "/api/v1/log", {"text": "coffee"}).json()["operation"]
    assert post(api, "/api/v1/undo", {"operation": operation}).json() == {"ok": True, "message": "Undone"}
    assert not Entry.objects.exists()
    assert post(api, "/api/v1/undo", {"operation": operation}).status_code == 400  # already undone
    assert post(api, "/api/v1/undo", {"operation": 999}).status_code == 404


def test_the_api_can_only_undo_its_own_operations(api):
    from tracking import logbook
    from tracking.logbook import TrackerLog

    web = logbook.apply([TrackerLog(tracker="coffee")], source="web")
    assert post(api, "/api/v1/undo", {"operation": web.pk}).status_code == 404 and Entry.objects.count() == 1


def test_today(api):
    post(api, "/api/v1/log", {"text": "no alcohol, screen 3h20"})
    body = api.get("/api/v1/today").json()
    assert body["day"] == days.today().isoformat()
    assert body["values"]["tracker:alcohol"] == {"name": "Alcohol", "value": 0.0, "text": "0"}
    assert body["values"]["tracker:screen"]["text"] == "3h 20m"
    assert body["values"]["tracker:mood"] == {"name": "Mood", "value": None, "text": None}  # unlogged is null, not 0
    assert "Mood" in body["unlogged"] and "Alcohol" not in body["unlogged"]


def test_wrong_tokens_are_rate_limited():
    client = Client(headers={"Authorization": "Bearer guess"})
    codes = [post(client, "/api/v1/log", {"text": "coffee"}).status_code for _ in range(25)]
    assert codes[:20] == [401] * 20 and codes[-1] == 429


def test_only_the_documented_methods(api):
    assert api.get("/api/v1/log").status_code == 405 and api.post("/api/v1/today").status_code == 405
