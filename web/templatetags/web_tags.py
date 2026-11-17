import json

from django import template

register = template.Library()


@register.filter
def tojson(value):
    """For data-* attributes; Django's autoescaping makes it attribute-safe."""
    return json.dumps(value)


@register.filter
def pct(ratio):
    return f"{(ratio or 0) * 100:.0f}%"


@register.filter
def money(value):
    from tracking import fmt

    return fmt.money(value)


@register.simple_tag
def main_currency():
    """Symbol and decimal places of the acting user's currency, for the charts."""
    from tracking import fmt, owner
    from tracking.models import AppSettings

    code = AppSettings.load().default_currency if owner.current_or_none() else "CAD"
    return {"symbol": fmt.symbol(code), "places": fmt.places(code)}


@register.filter
def metric_value(value, fmt_name):
    from tracking import fmt

    return fmt.value(fmt_name, value)
