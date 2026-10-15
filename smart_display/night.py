"""Night window (Ticket #2). Pure helpers, mirrored 1:1 in app.js.

A window is two ``HH:MM`` wall-clock strings in the configured timezone. It
may wrap midnight (``22:30`` → ``06:00``). An empty or unparsable bound
disables the window, which keeps the panel in day mode rather than guessing.
"""
from __future__ import annotations

from datetime import datetime


def parse_hhmm(value: str | None) -> int | None:
    """``"22:30"`` → 1350 (minutes since midnight); ``None`` when invalid."""
    if not value:
        return None
    text = str(value).strip()
    if ":" not in text:
        return None
    hours_text, minutes_text = text.split(":", 1)
    if not (hours_text.isdigit() and minutes_text.isdigit()):
        return None
    hours, minutes = int(hours_text), int(minutes_text)
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes


def is_night_minutes(minute_of_day: int, start: int | None, end: int | None) -> bool:
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= minute_of_day < end
    # Wraps midnight.
    return minute_of_day >= start or minute_of_day < end


def is_night(now_local: datetime, night_from: str | None, night_to: str | None) -> bool:
    return is_night_minutes(
        now_local.hour * 60 + now_local.minute,
        parse_hhmm(night_from),
        parse_hhmm(night_to),
    )


def window_for_ui(night_from: str | None, night_to: str | None) -> dict[str, str] | None:
    """What the template hands to app.js: both bounds, or ``None`` when the
    window is disabled so the client skips the computation entirely."""
    start = parse_hhmm(night_from)
    end = parse_hhmm(night_to)
    if start is None or end is None or start == end:
        return None
    return {"from": str(night_from).strip(), "to": str(night_to).strip()}
