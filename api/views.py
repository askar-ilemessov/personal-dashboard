"""HTTP API for automations such as iOS Shortcuts.

Everything goes through the same parser and logging service as the web
app and the bot. Authentication is a bearer token created in Settings.
"""

from django.contrib.auth.decorators import login_not_required
from rest_framework.decorators import api_view
from rest_framework.response import Response

from parsing import parser
from tracking import days, fmt, logbook, metrics
from tracking.logbook import LogError
from tracking.models import Operation

SOURCE = "shortcut"


@login_not_required
@api_view(["POST"])
def log(request):
    """Log something described in text, e.g. {"text": "woke"}.

    The quick-log grammar is always used. Send "llm": true to let Claude
    interpret sentences the grammar can't read; there is no confirm step
    here, so that is off unless asked for.
    """
    text = str(request.data.get("text", "")).strip()
    if not text:
        return Response({"ok": False, "errors": ['Send JSON like {"text": "woke"}.']}, status=400)
    result = parser.interpret(text, use_llm=request.data.get("llm") in (True, "true", "1", 1))
    if not result.ok:
        return Response({"ok": False, "errors": result.errors or ["Nothing to log."]}, status=400)
    try:
        operation = logbook.apply(result.intents, source=SOURCE, raw_text=text)
    except LogError as e:
        return Response({"ok": False, "errors": [str(e)]}, status=400)
    return Response({"ok": True, "logged": result.lines, "operation": operation.pk,
                     "message": "; ".join(result.lines)}, status=201)


@login_not_required
@api_view(["POST"])
def undo(request):
    """Undo a previous log: {"operation": 123}."""
    operation = Operation.objects.filter(pk=request.data.get("operation") or 0, source=SOURCE).first()
    if operation is None:
        return Response({"ok": False, "errors": ["No such operation from this API."]}, status=404)
    try:
        logbook.undo(operation)
    except LogError as e:
        return Response({"ok": False, "errors": [str(e)]}, status=400)
    return Response({"ok": True, "message": "Undone"})


@login_not_required
@api_view(["GET"])
def today(request):
    """Today's values and what is still unlogged, for widgets and Shortcuts."""
    day = days.today()
    values = {}
    for m in metrics.catalog():
        value = metrics.value(m.key, day)
        values[m.key] = {"name": m.name, "value": None if value is None else float(value),
                         "text": None if value is None else fmt.value(m.fmt, value)}
    missing = [metrics.get(key).name for key in metrics.unlogged(day)]
    return Response({"day": day.isoformat(), "values": values, "unlogged": missing})
