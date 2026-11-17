from datetime import UTC, date, datetime, time

import pytest

from conftest import local
from tracking import days


def test_before_boundary_belongs_to_previous_day(configured):
    assert days.logical_day(local(2026, 11, 15, 3, 59)) == date(2026, 11, 14)
    assert days.logical_day(local(2026, 11, 15, 4, 0)) == date(2026, 11, 15)
    assert days.logical_day(local(2026, 11, 15, 23, 59)) == date(2026, 11, 15)


def test_timezones_people_search_for():
    assert days.zone_name(" Astana ") == days.zone_name("Asia/Almaty") == "Asia/Almaty"
    assert datetime(2026, 11, 16, 12, tzinfo=days.ZoneInfo("Asia/Almaty")).utcoffset().seconds == 5 * 3600
    with pytest.raises(ValueError, match="is not a timezone"):
        days.zone_name("Mars/Olympus")


def test_boundary_is_configurable(configured):
    configured.day_start = time(0, 0)
    assert days.logical_day(local(2026, 11, 15, 0, 30), configured) == date(2026, 11, 15)
    configured.day_start = time(5, 30)
    assert days.logical_day(local(2026, 11, 15, 5, 29), configured) == date(2026, 11, 14)


def test_utc_input_is_converted_to_local_time(configured):
    # 07:00 UTC on the 15th is 02:00 in Toronto (EST), so still the 14th.
    assert days.logical_day(datetime(2026, 11, 15, 7, 0, tzinfo=UTC)) == date(2026, 11, 14)


def test_naive_input_is_treated_as_local(configured):
    assert days.logical_day(datetime(2026, 11, 15, 2, 0)) == date(2026, 11, 14)


def test_dst_change_nights(configured):
    # Clocks go back on 2026-11-01 and forward on 2026-03-08 in Toronto.
    assert days.logical_day(local(2026, 11, 1, 1, 30)) == date(2026, 10, 31)
    assert days.logical_day(local(2026, 11, 1, 4, 30)) == date(2026, 11, 1)
    assert days.logical_day(local(2026, 3, 8, 3, 30)) == date(2026, 3, 7)
    assert days.utc_offset_min(local(2026, 7, 1)) == -240
    assert days.utc_offset_min(local(2026, 12, 1)) == -300
