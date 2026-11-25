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


# Amounts in the main currency get its symbol; other currencies show their code.
SYMBOLS = {"CAD": "$", "USD": "$", "AUD": "$", "NZD": "$", "MXN": "$",
           "EUR": "€", "GBP": "£", "JPY": "¥", "INR": "₹", "KZT": "₸"}
WHOLE = {"JPY", "KZT"}  # quoted without cents in everyday use


def symbol(currency: str) -> str:
    return SYMBOLS.get(currency, f"{currency} ")


def places(currency: str) -> int:
    return 0 if currency in WHOLE else 2


def money(value, currency: str | None = None, default_currency: str | None = None) -> str:
    """Without a default currency, uses the acting user's main currency."""
    if default_currency is None:
        from tracking.models import AppSettings

        default_currency = AppSettings.load().default_currency
    currency = currency or default_currency
    amount = f"{Decimal(str(value)):,.{places(currency)}f}"
    return f"{symbol(currency)}{amount}" if currency == default_currency else f"{amount} {currency}"


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


def sleep_times(stretches, tz) -> str:
    """"23:30 → 07:10", or "22:00 → 01:00, 01:30 → 06:40" for a night with a break."""
    return ", ".join(f"{clock(n.bedtime, tz) + ' → ' if n.bedtime else ''}{clock(n.wake_time, tz)}" for n in stretches)
