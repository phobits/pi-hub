"""German label helpers for server-rendered UI (Plan B14).

Everything here is stdlib-only. The Pi may not ship a de_DE.UTF-8 locale, so
we hardcode the handful of labels we actually need instead of relying on
``locale.setlocale`` at runtime.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from smart_display.calendar_layout import GERMAN_WEEKDAYS_LONG


GERMAN_MONTHS_LONG = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def format_initial_clock(
    iana_name: str, *, now: datetime | None = None
) -> dict[str, str]:
    """Return the values needed to server-render the hero clock.

    ``now`` is injectable so tests can freeze time. All strings are German
    with real umlauts — no ``--:--`` placeholder on cold reload.
    """
    zone = ZoneInfo(iana_name)
    current = (now or datetime.now()).astimezone(zone)
    weekday = GERMAN_WEEKDAYS_LONG[current.weekday()]
    month = GERMAN_MONTHS_LONG[current.month - 1]
    return {
        "time": current.strftime("%H:%M"),
        "seconds": current.strftime("%S"),
        "date": f"{weekday}, {current.day}. {month}",
    }
