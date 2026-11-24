"""People (superusers only): how each account uses the app.

Usage only: when someone was last active, how many things they logged per
day, from where, and how far setup got. Never what they logged. This is
one of the few places that reads across accounts, so it uses the unscoped
`all_users` managers and only ever aggregates."""

from collections import Counter, defaultdict
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Max
from django.shortcuts import render
from django.utils import timezone

from tracking import days
from tracking.models import AppSettings, Operation, Source, TelegramLink

WINDOW = 7


@user_passes_test(lambda u: u.is_superuser)
def people(request):
    now = timezone.now()
    since = now - timedelta(days=30)
    users = list(get_user_model().objects.filter(is_active=True).order_by("username"))
    settings = {s.user_id: s for s in AppSettings.all_users.filter(user__in=users)}
    telegram = set(TelegramLink.all_users.filter(user__in=users, telegram_id__isnull=False).values_list("user_id", flat=True))
    last_op = dict(Operation.all_users.filter(user__in=users, undone_at__isnull=True)
                   .values_list("user").annotate(last=Max("created_at")).values_list("user", "last"))
    sources = defaultdict(Counter)
    per_day = defaultdict(Counter)
    ops = Operation.all_users.filter(user__in=users, undone_at__isnull=True, created_at__gte=since)
    for user_id, created, source in ops.values_list("user", "created_at", "source"):
        sources[user_id][source] += 1
        s = settings.get(user_id)
        per_day[user_id][days.logical_day(created, s) if s else created.date()] += 1

    labels = dict(Source.choices)
    rows = []
    for u in users:
        s = settings.get(u.pk)
        today = days.today(s) if s else now.date()
        week = [today - timedelta(days=WINDOW - 1 - i) for i in range(WINDOW)]
        counts = [per_day[u.pk].get(d, 0) for d in week]
        peak = max(counts) or 1
        last = max((t for t in (last_op.get(u.pk), u.last_login) if t), default=None)
        invited = not u.has_usable_password()
        rows.append({
            "user": u, "you": u == request.user, "last": last, "invited": invited,
            "entries": sum(counts), "bars": [{"day": d, "n": n, "h": round(n / peak * 100)} for d, n in zip(week, counts)],
            "sources": ", ".join(labels.get(k, k) for k, _ in sources[u.pk].most_common()) or "—",
            "telegram": u.pk in telegram,
            "setup": "Invite sent, not signed in yet" if invited else
                     f"Setting up: {s.setup_step}" if s and s.setup_step else "Set up",
        })
    active = [r for r in rows if r["entries"]]
    return render(request, "web/people.html", {
        "nav": "settings", "rows": rows,
        "tiles": [
            {"label": "Active this week", "value": f"{len(active)} of {len(rows)}"},
            {"label": "Things logged this week", "value": str(sum(r["entries"] for r in rows))},
            {"label": "Telegram connected", "value": f"{sum(1 for r in rows if r['telegram'])} of {len(rows)}"},
            {"label": "Waiting to sign in", "value": str(sum(1 for r in rows if r["invited"]))},
        ],
    })
