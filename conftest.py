from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tracking import defaults
from tracking.models import AppSettings

TZ = ZoneInfo("America/Toronto")


def local(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


@pytest.fixture(autouse=True)
def configured(db):
    defaults.install()
    s = AppSettings.load()
    s.timezone = "America/Toronto"
    s.save()
    return s
