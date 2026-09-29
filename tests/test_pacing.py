from datetime import datetime
from zoneinfo import ZoneInfo

from leadmap.pacing import seconds_until_window

TZ = ZoneInfo("Asia/Tehran")


def at(h, m):
    return datetime(2026, 9, 25, h, m, tzinfo=TZ)


def test_window_wrapping_midnight():
    w = ((8, 0), (1, 0))
    assert seconds_until_window(at(12, 0), *w) == 0
    assert seconds_until_window(at(0, 30), *w) == 0
    assert seconds_until_window(at(23, 59), *w) == 0
    assert seconds_until_window(at(1, 0), *w) == 7 * 3600
    assert seconds_until_window(at(7, 30), *w) == 1800


def test_window_same_day():
    w = ((9, 0), (18, 0))
    assert seconds_until_window(at(10, 0), *w) == 0
    assert seconds_until_window(at(18, 0), *w) == 15 * 3600
