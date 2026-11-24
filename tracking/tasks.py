"""Reading a typed task: "call bank fri" -> ("Call bank", Friday)."""

import re
from datetime import date, timedelta

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DUE = re.compile(
    r"\s+(?:by\s+|on\s+|due\s+)?(today|tonight|tomorrow|tmrw|"
    r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"(\d{1,2})[./-](\d{1,2})|(\d{4})-(\d{2})-(\d{2}))$",
    re.IGNORECASE,
)


def parse(text: str, today: date) -> tuple[str, date | None]:
    """Title and due date. A trailing day word sets the date: a weekday
    means the next one (today counts), "tomorrow" the day after. A short
    date like 24.12 is read day first and must be a real date; one already
    past means next year."""
    text = " ".join(text.split())
    m = _DUE.search(text)
    due = None
    if m:
        word = m[1].lower()
        if word in ("today", "tonight"):
            due = today
        elif word in ("tomorrow", "tmrw"):
            due = today + timedelta(days=1)
        elif m[4]:
            due = _safe(int(m[4]), int(m[5]), int(m[6]))
        elif m[2]:
            due = _safe(today.year, int(m[3]), int(m[2]))
            if due and due < today:
                due = _safe(today.year + 1, int(m[3]), int(m[2]))
        else:
            target = WEEKDAYS.index(word[:3])
            due = today + timedelta(days=(target - today.weekday()) % 7)
        if due is not None:
            text = text[: m.start()].strip()
    title = text[:1].upper() + text[1:] if text else ""
    return title[:200], due


def _safe(y, mo, d) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None
