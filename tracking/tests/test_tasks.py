from datetime import date

import pytest

from tracking.tasks import parse

MON = date(2026, 11, 23)


@pytest.mark.parametrize("text,title,due", [
    ("call the bank", "Call the bank", None),
    ("call the bank fri", "Call the bank", date(2026, 11, 27)),
    ("call the bank friday", "Call the bank", date(2026, 11, 27)),
    ("pay rent on mon", "Pay rent", MON),  # today counts as the next Monday
    ("pay rent sunday", "Pay rent", date(2026, 11, 29)),
    ("book dentist tomorrow", "Book dentist", date(2026, 11, 24)),
    ("water plants today", "Water plants", MON),
    ("renew passport 24.12", "Renew passport", date(2026, 12, 24)),
    ("tax return 15/4", "Tax return", date(2027, 4, 15)),  # already past this year
    ("visa 2027-02-01", "Visa", date(2027, 2, 1)),
    ("party 31.02", "Party 31.02", None),  # not a real date: left in the title
    ("   buy   milk  ", "Buy milk", None),
    ("friday", "Friday", None),  # a lone day word is the title, not a date
])
def test_parse(text, title, due):
    assert parse(text, MON) == (title, due)
