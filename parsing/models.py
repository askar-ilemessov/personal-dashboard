from django.db import models
from django.utils import timezone

from tracking.owner import OwnedModel


class Draft(OwnedModel):
    """A parsed message waiting for the user to confirm it.

    The preview the user approves and what gets saved must be identical, so
    the interpretation is stored here instead of asking the model twice.
    `items` is the model's structured output; None means the text was
    understood by the deterministic grammar and is simply re-parsed."""

    text = models.TextField()
    items = models.JSONField(null=True, blank=True)
    source = models.CharField(max_length=16)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.text[:60]
