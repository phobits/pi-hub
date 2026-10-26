"""One display contract for weather conditions from two code families (#22).

Open-Meteo reports WMO weather interpretation codes; GeoSphere's C-LAEF
reports its own weather symbol ``sy``. The two are **not** interchangeable —
``sy`` 3 is "partly cloudy", WMO 3 is "overcast" — so each family has its own
table and both map onto the same small set of icon keys the frontend draws:

    clear, clear-night, partly-cloudy, partly-cloudy-night, cloudy, fog,
    rain, sleet, snow, storm, unknown

The ``sy`` table is GeoSphere's own code list (Hilfestellung zur Umstellung
auf nwp-v2-1h-1km, public.hub.geosphere.at/public/resources/misc/
Hilfestellung_DE.pdf; the dataset metadata has no code list for it). A code
in neither table, or a missing one, is ``unknown`` — never a sun. Only the
clear and partly-cloudy icons have a night variant; rain at night is still
rain.
"""
from __future__ import annotations

UNKNOWN = ("unknown", "")

# WMO 4677 subset Open-Meteo documents for ``weather_code``.
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("clear", "Klar"),
    1: ("clear", "Überwiegend klar"),
    2: ("partly-cloudy", "Leicht bewölkt"),
    3: ("cloudy", "Bedeckt"),
    45: ("fog", "Nebel"),
    48: ("fog", "Reifnebel"),
    51: ("rain", "Leichter Nieselregen"),
    53: ("rain", "Nieselregen"),
    55: ("rain", "Starker Nieselregen"),
    56: ("sleet", "Gefrierender Nieselregen"),
    57: ("sleet", "Starker gefrierender Nieselregen"),
    61: ("rain", "Leichter Regen"),
    63: ("rain", "Regen"),
    65: ("rain", "Starker Regen"),
    66: ("sleet", "Gefrierender Regen"),
    67: ("sleet", "Starker gefrierender Regen"),
    71: ("snow", "Leichter Schnee"),
    73: ("snow", "Schnee"),
    75: ("snow", "Starker Schneefall"),
    77: ("snow", "Schneegriesel"),
    80: ("rain", "Leichte Schauer"),
    81: ("rain", "Regenschauer"),
    82: ("rain", "Starke Schauer"),
    85: ("snow", "Leichte Schneeschauer"),
    86: ("snow", "Starke Schneeschauer"),
    95: ("storm", "Gewitter"),
    96: ("storm", "Gewitter mit Hagel"),
    99: ("storm", "Starkes Gewitter"),
}

# GeoSphere ``sy`` (C-LAEF weather symbol), see the module docstring.
GEOSPHERE_SYMBOLS: dict[int, tuple[str, str]] = {
    1: ("clear", "Klar"),
    2: ("clear", "Überwiegend klar"),
    3: ("partly-cloudy", "Teilweise bewölkt"),
    4: ("cloudy", "Überwiegend bewölkt"),
    5: ("cloudy", "Bedeckt"),
    6: ("fog", "Bodennebel"),
    # Low stratus is a cloud deck, not fog at the ground.
    7: ("cloudy", "Hochnebel"),
    8: ("rain", "Leichter Regen"),
    9: ("rain", "Regen"),
    10: ("rain", "Starker Regen"),
    11: ("sleet", "Schneeregen"),
    12: ("sleet", "Schneeregen"),
    13: ("sleet", "Schneeregen"),
    14: ("snow", "Leichter Schneefall"),
    15: ("snow", "Schneefall"),
    16: ("snow", "Starker Schneefall"),
    17: ("rain", "Regenschauer"),
    18: ("rain", "Regenschauer"),
    19: ("rain", "Starke Regenschauer"),
    20: ("sleet", "Schneeregenschauer"),
    21: ("sleet", "Schneeregenschauer"),
    22: ("sleet", "Schneeregenschauer"),
    23: ("snow", "Schneeschauer"),
    24: ("snow", "Schneeschauer"),
    25: ("snow", "Starke Schneeschauer"),
    26: ("storm", "Gewitter"),
    27: ("storm", "Gewitter"),
    28: ("storm", "Starkes Gewitter"),
    29: ("storm", "Gewitter mit Schneeregen"),
    30: ("storm", "Starkes Gewitter mit Schneeregen"),
    31: ("storm", "Gewitter mit Schnee"),
    32: ("storm", "Starkes Gewitter mit Schnee"),
}

_NIGHT_VARIANTS = {"clear": "clear-night", "partly-cloudy": "partly-cloudy-night"}


def _with_daylight(entry: tuple[str, str], is_day: bool | None) -> tuple[str, str]:
    icon, label = entry
    if is_day is False:
        icon = _NIGHT_VARIANTS.get(icon, icon)
    return icon, label


def _as_code(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # GeoSphere sends 3.0
    except (TypeError, ValueError):
        return None
    if number != number or number != int(number):  # NaN or a fraction
        return None
    return int(number)


def wmo_condition(code: object, is_day: bool | None = True) -> tuple[str, str]:
    """(icon, label) for an Open-Meteo WMO code. ``is_day=None`` means "a
    daily summary" and always gets the day icon."""
    number = _as_code(code)
    entry = WMO_CODES.get(number) if number is not None else None
    return _with_daylight(entry, is_day) if entry else UNKNOWN


def geosphere_condition(symbol: object, is_day: bool | None = True) -> tuple[str, str]:
    """(icon, label) for a GeoSphere ``sy`` weather symbol."""
    number = _as_code(symbol)
    entry = GEOSPHERE_SYMBOLS.get(number) if number is not None else None
    return _with_daylight(entry, is_day) if entry else UNKNOWN


# GeoSphere warn API ``warntypid`` / ``warnstufeid``. The display name
# ("Orange Windwarnung") is built here and sent with every warning, so the
# frontend has no table of its own to keep in step.
WARN_TYPES = {
    1: "Wind",
    2: "Regen",
    3: "Schnee",
    4: "Glatteis",
    5: "Gewitter",
    6: "Hitze",
    7: "Kälte",
}
# Level: (label, adjective before "…warnung") — one table, so a new level
# cannot be accepted by the parser and lose its colour in the title.
_WARN_LEVEL_NAMES = {1: ("Gelb", "Gelbe"), 2: ("Orange", "Orange"), 3: ("Rot", "Rote")}
WARN_LEVELS = {level: label for level, (label, _adjective) in _WARN_LEVEL_NAMES.items()}


def _code(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def warning_title(warn_type: object, level: object) -> str:
    """"Orange Windwarnung"; an unknown type is a plain "Wetterwarnung",
    an unknown level drops the colour rather than guessing one."""
    kind = WARN_TYPES.get(_code(warn_type))
    name = f"{kind}warnung" if kind else "Wetterwarnung"
    names = _WARN_LEVEL_NAMES.get(_code(level))
    return f"{names[1]} {name}" if names else name


# Duden abbreviations, Monday first (date.weekday()); app.js mirrors them.
WEEKDAY_SHORT = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")
