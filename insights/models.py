from django.db import models
from django.utils import timezone

from tracking.owner import OwnedModel


class WeeklyReview(OwnedModel):
    """One review per week (Monday to Sunday). `facts` is what the stats
    engine computed; the text only words those facts."""

    week_start = models.DateField()
    facts = models.JSONField()
    summary = models.TextField()
    observations = models.JSONField(default=list)
    suggestion = models.TextField()
    written_by = models.CharField(max_length=16, default="template")  # "claude" or "template"
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True, help_text="When it was delivered on Telegram")

    class Meta:
        ordering = ["-week_start"]
        constraints = [models.UniqueConstraint(fields=["user", "week_start"], name="one_review_per_week")]

    def __str__(self):
        return f"Week of {self.week_start}"
