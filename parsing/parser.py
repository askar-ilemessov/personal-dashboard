"""The shared parser: deterministic grammar first, Claude as the fallback.

Used by the web command palette, the Telegram bot and the Shortcuts
endpoint. Callers show `lines` as a preview, store a Draft, and save the
draft's intents when the user confirms.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings as django_settings
from django.utils import timezone

from tracking import logbook
from tracking.logbook import LogError
from tracking.models import AppSettings

from . import grammar, llm
from .describe import describe
from .models import Draft

DRAFT_LIFETIME = timedelta(days=1)


@dataclass
class Interpretation:
    text: str
    intents: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tier: str = "grammar"  # or "llm"
    ignored: str = ""  # text the model could not log
    items: list | None = None  # the model's raw items, kept for the draft

    @property
    def ok(self) -> bool:
        return bool(self.intents) and not self.errors

    @property
    def lines(self) -> list[str]:
        settings = AppSettings.load()
        return [describe(i, settings) for i in self.intents]


def llm_enabled() -> bool:
    return bool(django_settings.ANTHROPIC_API_KEY)


def interpret(text: str, now: datetime | None = None, use_llm: bool = True, client=None) -> Interpretation:
    """Parse and validate. Nothing is saved."""
    now = now or timezone.now()
    text = (text or "").strip()
    parsed = grammar.parse(text, now=now)
    result = Interpretation(text=text, intents=parsed.intents, errors=list(parsed.errors))

    if not parsed.ok and text and use_llm and (llm_enabled() or client):
        settings = AppSettings.load()
        try:
            data = llm.call(text, now, settings, client=client)
        except llm.LlmUnavailable as e:
            result.errors.append(f"Claude couldn't help either: {e}.")
            return result
        intents, problems = llm.to_intents(data["items"], now, settings)
        result = Interpretation(text=text, intents=intents, errors=problems, tier="llm",
                                ignored=(data.get("unparsed") or "").strip(), items=data["items"])
        if not intents and not problems:
            result.errors.append("I couldn't find anything to log in that.")

    if result.intents and not result.errors:
        try:
            logbook.check(result.intents)  # same validation as saving
        except LogError as e:
            result.errors.append(str(e))
    return result


def save_draft(result: Interpretation, source: str) -> Draft:
    Draft.objects.filter(created_at__lt=timezone.now() - DRAFT_LIFETIME).delete()
    return Draft.objects.create(text=result.text, items=result.items, source=source)


def draft_intents(draft: Draft, now: datetime | None = None) -> list:
    """Rebuild exactly what the preview showed, without asking the model again."""
    now = now or timezone.now()
    if draft.items is None:
        parsed = grammar.parse(draft.text, now=now)
        if not parsed.ok:
            raise LogError(parsed.errors[0] if parsed.errors else "Nothing to log.")
        return parsed.intents
    intents, problems = llm.to_intents(draft.items, now, AppSettings.load())
    if problems or not intents:
        raise LogError(problems[0] if problems else "Nothing to log.")
    return intents
