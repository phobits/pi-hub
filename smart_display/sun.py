"""Sunrise and sunset for the configured location (#22), stdlib only.

The detail screen's hourly icons need a day and a night variant, and neither
GeoSphere series carries one. This is NOAA's "general solar position"
approximation (fractional-year series for the equation of time and the
declination, 90.833° zenith for refraction and the solar disc) — within
three minutes of Open-Meteo's times for Vienna (checked 2026-10-26), which is
plenty for choosing between a sun and a moon. No network, so it works for
cached data after a restart.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

_ZENITH_DEG = 90.833


def _day_terms(day: date) -> tuple[float, float]:
    """Equation of time (minutes) and declination (radians) at noon UTC."""
    gamma = 2 * math.pi / 365 * (day.timetuple().tm_yday - 1)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    return eqtime, decl


def sun_times(day: date, latitude: float, longitude: float) -> tuple[datetime | None, datetime | None]:
    """Sunrise and sunset of ``day`` (a UTC calendar date) as aware UTC
    datetimes. ``(None, None)`` on a polar day or night."""
    eqtime, decl = _day_terms(day)
    lat = math.radians(latitude)
    arg = math.cos(math.radians(_ZENITH_DEG)) / (math.cos(lat) * math.cos(decl)) - math.tan(lat) * math.tan(decl)
    if arg < -1 or arg > 1:
        return None, None
    hour_angle = math.degrees(math.acos(arg))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    sunrise = midnight + timedelta(minutes=720 - 4 * (longitude + hour_angle) - eqtime)
    sunset = midnight + timedelta(minutes=720 - 4 * (longitude - hour_angle) - eqtime)
    return sunrise, sunset


def is_daylight(moment: datetime, latitude: float, longitude: float) -> bool:
    """Whether the sun is up at ``moment`` (aware). A polar day counts as day,
    a polar night as night."""
    utc = moment.astimezone(timezone.utc)
    sunrise, sunset = sun_times(utc.date(), latitude, longitude)
    if sunrise is None or sunset is None:
        _eqtime, decl = _day_terms(utc.date())
        # Same hemisphere as the declination → the sun never sets.
        return (latitude >= 0) == (decl >= 0)
    return sunrise <= utc < sunset
