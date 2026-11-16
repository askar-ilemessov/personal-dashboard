"""Health check for the host (Railway calls it before switching traffic to a
new deploy). No login, and nothing user-specific: it only proves the app
starts and can reach the database."""

from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError, connection
from django.http import HttpResponse
from django.views.decorators.cache import never_cache


@login_not_required
@never_cache
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
