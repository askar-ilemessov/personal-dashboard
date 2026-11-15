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


@register.filter
def metric_value(value, fmt_name):
    from tracking import fmt

    return fmt.value(fmt_name, value)
