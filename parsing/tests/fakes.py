"""A stand-in for the Anthropic client, so tests never use the network."""

import json
from types import SimpleNamespace

BLANK = {
    "kind": "tracker", "day_offset": None, "date": None, "time": None, "tracker": "none", "value": None,
    "replace": False, "amount": None, "currency": None, "category": "none", "bedtime": None, "wake_time": None,
    "workout_type": "none", "duration_min": None, "text": None, "tags": [],
}


def item(kind, **fields):
    return {**BLANK, "kind": kind, **fields}


class FakeClaude:
    """Answers every request with the given items (or raw text / stop reason)."""

    def __init__(self, items=(), unparsed="", raw=None, stop_reason="end_turn", error=None):
        self.body = raw if raw is not None else json.dumps({"items": list(items), "unparsed": unparsed})
        self.stop_reason = stop_reason
        self.error = error
        self.requests = []
        self.messages = self

    def with_options(self, **options):
        self.options = options
        return self

    def create(self, **request):
        self.requests.append(request)
        if self.error:
            raise self.error
        # Thinking comes first on current models; the parser must read blocks by type.
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self.body)]
        return SimpleNamespace(stop_reason=self.stop_reason, content=content)
