"""Human-readable values, shared by the web UI, the bot and summaries."""

from datetime import datetime
from decimal import Decimal


def number(value) -> str:
    """12 -> "12", 12.50 -> "12.5"."""
    d = Decimal(str(value))
    text = f"{d:,.2f}".rstrip("0").rstrip(".")
    return text or "0"


def minutes(value) -> str:
    """450 -> "7h 30m", 45 -> "45m"."""
    total = round(float(value))
    sign = "−" if total < 0 else ""
    hours, mins = divmod(abs(total), 60)
    if hours and mins:
        return f"{sign}{hours}h {mins:02d}m"
    return f"{sign}{hours}h" if hours else f"{sign}{mins}m"


def money(value, currency: str = "CAD", default_currency: str = "CAD") -> str:
    amount = f"{Decimal(str(value)):,.2f}"
    return f"${amount}" if currency == default_currency else f"{amount} {currency}"


def clock(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def value(fmt: str, v) -> str:
    """Format a metric value by its format name. None is "not logged"."""
    if v is None:
        return "—"
    if fmt == "minutes":
        return minutes(v)
    if fmt in ("clock", "bedtime"):  # minutes after midnight, or after noon for bedtimes
        total = round(float(v)) + (720 if fmt == "bedtime" else 0)
        return f"{total // 60 % 24:02d}:{total % 60:02d}"
    if fmt == "money":
        return money(v)
    if fmt == "habit":
        return "Done" if v >= 1 else "Not done"
    if fmt == "percent":
        return f"{round(float(v) * 100)}%"
    return number(v)
