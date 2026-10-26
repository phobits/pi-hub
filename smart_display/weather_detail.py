"""What /api/state sends for the weather detail screen (#22).

The store keeps every source as it arrived. Two decisions are made here, at
request time, on the plain dicts of ``DashboardState.to_dict`` so the poll
stays cheap:

* ``select_hourly`` picks **one** hourly series — C-LAEF, or Open-Meteo when
  C-LAEF has nothing current. A series is never patched hour by hour from the
  other model.
* ``prepare_warnings`` sorts the warnings and gives each one the moment it
  stops being shown (``visible_until``), so the frontend only compares
  timestamps — an expired warning disappears on time without a new fetch —
  and its display name (``title``), so the frontend has no name table.

Both functions are pure; ``app.js`` mirrors only the comparison with the
clock (``visibleWarnings``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from smart_display.weather_codes import warning_title

# Usable last-good data, in the order a source is trusted. ``error`` still
# carries the last good series and its age, so it beats a source with none.
_USABLE = ("ok", "stale", "error")

# A warning whose end is missing or not after its begin gets no open-ended
# visibility. It is shown while the warn API keeps listing it and for at most
# this long after the last successful fetch that did — so a broken entry
# cannot pin an alarm to the screen through an outage.
UNBOUNDED_WARNING_HOLD = timedelta(hours=3)

# The screensaver shows warnings that are active or begin within this window.
SCREENSAVER_LOOKAHEAD = timedelta(hours=24)


def parse_utc(value: Any) -> datetime | None:
    """An ISO timestamp ("2026-10-26T09:00+00:00", "…Z") as aware UTC. The
    one parser for provider and request time; an unzoned stamp is ambiguous
    and gives None rather than a guess."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc)


_parse = parse_utc


def _covers(series: dict, now: datetime) -> bool:
    """Has at least one hour that has not ended yet."""
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    for point in series.get("points") or []:
        moment = _parse(point.get("time"))
        if moment is not None and moment > hour_start:
            return True
    return False


def select_hourly(state: dict, now: datetime) -> dict:
    """The hourly series the detail screen draws, with ``fallback`` = True
    when it is Open-Meteo standing in for an enabled GeoSphere source."""
    geosphere = state.get("weather_hourly_geosphere") or {}
    openmeteo = state.get("weather_hourly_openmeteo") or {}
    geo_status = (geosphere.get("snapshot") or {}).get("status")
    om_status = (openmeteo.get("snapshot") or {}).get("status")

    def pick(series: dict, *, fallback: bool) -> dict:
        return {**series, "fallback": fallback, "primary_status": geo_status}

    # Open-Meteo stands in for a GeoSphere that failed; one that is switched
    # off or has not answered yet (``empty``, boot grace) is no outage.
    outage = geo_status in ("ok", "stale", "error")
    for status_set in (("ok", "stale"), ("error",)):
        if geo_status in status_set and _covers(geosphere, now):
            return pick(geosphere, fallback=False)
        if om_status in status_set and _covers(openmeteo, now):
            return pick(openmeteo, fallback=outage)
    # Nothing current anywhere. Report the primary source's own state —
    # unless it is switched off, then Open-Meteo's. Old points go out too,
    # and the frontend never draws an hour that has passed.
    if geo_status == "disabled" or geo_status is None:
        return pick(openmeteo, fallback=False)
    return pick(geosphere, fallback=False)


def warning_visible_until(item: dict, fetched_at: datetime | None) -> datetime | None:
    """End of a warning's visibility (exclusive). The official end when it
    is valid; otherwise the bounded hold after the last confirmation. None
    when neither exists — then it is not shown at all."""
    ends = _parse(item.get("ends_at"))
    begins = _parse(item.get("begins_at"))
    if ends is not None and (begins is None or ends > begins):
        return ends
    if fetched_at is None:
        return None
    return fetched_at + UNBOUNDED_WARNING_HOLD


def _sort_key(item: dict) -> tuple:
    level = item.get("level")
    begins = item.get("begins_at") or ""
    return (-(level or 0), begins, item.get("type") or 0, item.get("id") or "")


def prepare_warnings(section: dict, now: datetime) -> dict:
    """Sorted by official level, then begin; ended ones dropped. Each item
    gains ``visible_until`` (UTC ISO). A begin that is missing counts as
    already active — showing a warning early is the safe side."""
    fetched_at = _parse((section.get("snapshot") or {}).get("updated_at"))
    items = []
    for item in section.get("items") or []:
        until = warning_visible_until(item, fetched_at)
        if until is None or until <= now:
            continue
        items.append(
            {
                **item,
                "visible_until": until.isoformat(),
                "title": warning_title(item.get("type"), item.get("level")),
            }
        )
    items.sort(key=_sort_key)
    return {**section, "items": items}


def screensaver_warnings(section: dict, now: datetime) -> list[dict]:
    """The warnings the screensaver line names: visible now and already
    begun or beginning within SCREENSAVER_LOOKAHEAD. Mirrored by
    ``visibleWarnings`` in app.js, which runs it on every minute tick."""
    horizon = now + SCREENSAVER_LOOKAHEAD
    result = []
    for item in prepare_warnings(section, now)["items"]:
        begins = _parse(item.get("begins_at"))
        if begins is None or begins < horizon:
            result.append(item)
    return result
