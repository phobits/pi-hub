"""Next departures of a few lines from the Linz AG EFA (#27).

Read **on demand** like the pi5 status (#24): the frontend polls
``/api/transit`` only while the home screen is visible inside the departure
window, and this provider reads the EFA departure monitor at most every
``CACHE_TTL_SECONDS``. No scheduler job, no ``DashboardState`` section, no
``last_good.json`` write — departures are worthless after a reboot, and a
background job would break the ``/api/state`` ETag every minute.

The window is checked here too: outside it the answer is ``inactive`` and no
request leaves the device, whatever the frontend thinks the time is.

Every stop is fetched and judged on its own, so one failing stop keeps the
other line on screen. Anything the EFA sends that does not parse is dropped,
never filled in.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from smart_display.config import AppConfig, TransitStop
from smart_display.http_client import HttpClient
from smart_display.night import parse_hhmm
from smart_display.providers._fetch import Backoff, FetchError, fetch_json

logger = logging.getLogger(__name__)

# Several requests within this window share one fetch (and one failure).
# Shorter than the frontend's 30 s poll: equal, a poll landing a moment early
# would hit the cache and real-time data would arrive only every 60 s.
CACHE_TTL_SECONDS = 25.0
# After a failed fetch a stop keeps its last good departures this long, then
# it says "nicht verfügbar" instead of an hour-old timetable.
STALE_MAX_AGE_SECONDS = 600.0
# The departure monitor lists every line at the stop; at Schiffswerft the 27
# runs every few minutes, so 40 entries cover about 90 minutes of the 70.
DM_LIMIT = 40
# 40 departures are ~45 KB; anything near this cap is not a departure list.
MAX_BYTES = 512_000
# Sent per stop: the frontend shows ``departures`` of them and drops the ones
# that left between two polls (or while the data is stale) from the rest.
SPARE_DEPARTURES = 3
# A departure counts until the end of its minute ("jetzt"), unless a walk time
# says it cannot be reached anyway.
DEPARTED_GRACE = timedelta(seconds=60)


class TransitProvider:
    def __init__(
        self,
        config: AppConfig,
        *,
        http_client: HttpClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._http = http_client or HttpClient()
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._zone = ZoneInfo(config.app.timezone)
        self._lock = threading.Lock()
        self._last_result: dict[str, Any] | None = None
        self._last_result_at = float("-inf")
        # Per stop: (departures, monotonic time, ISO stamp) of the last good fetch.
        self._good: dict[int, tuple[list[dict[str, Any]], float, str]] = {}
        # Per stop: honours 429/5xx and Retry-After, like the weather providers.
        self._backoff = [
            Backoff(CACHE_TTL_SECONDS, monotonic=monotonic) for _ in config.transit.stops
        ]

    @property
    def enabled(self) -> bool:
        return bool(self._config.transit.stops) or self._config.app.demo_mode

    def ui_config(self) -> dict[str, Any] | None:
        """What app.js needs to know the window without asking: ``None`` when
        there are no departure rows at all."""
        if not self.enabled:
            return None
        transit = self._config.transit
        return {
            "stops": [{"line": s.line, "label": s.label} for s in self._stops()],
            "days": list(transit.days),
            "from": transit.window_from,
            "to": transit.window_to,
            "departures": transit.departures,
        }

    def in_window(self, now: datetime) -> bool:
        transit = self._config.transit
        local = now.astimezone(self._zone)
        if local.isoweekday() not in transit.days:
            return False
        start, end = parse_hhmm(transit.window_from), parse_hhmm(transit.window_to)
        if start is None or end is None:
            return False
        return start <= local.hour * 60 + local.minute < end

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return _envelope("disabled", None, None)
        now = self._utcnow()
        if not self.in_window(now):
            return _envelope("inactive", None, None)
        if not self._config.transit.stops:  # demo mode without real stops
            return _envelope("ok", {"stops": _demo_stops(now, self._zone)}, _stamp(now))

        with self._lock:
            tick = self._monotonic()
            if self._last_result is not None and tick - self._last_result_at < CACHE_TTL_SECONDS:
                return self._last_result
            stops = [
                self._stop_result(index, stop, now, tick)
                for index, stop in enumerate(self._config.transit.stops)
            ]
            result = _envelope(_overall(stops), {"stops": stops}, _newest(stops))
            self._last_result = result
            self._last_result_at = tick
            return result

    def _stops(self) -> list[TransitStop]:
        if self._config.transit.stops:
            return self._config.transit.stops
        return [TransitStop(s["stop_id"], s["line"], s["label"]) for s in DEMO_STOPS]

    def _stop_result(
        self, index: int, stop: TransitStop, now: datetime, tick: float
    ) -> dict[str, Any]:
        base = {"line": stop.line, "label": stop.label, "walk_minutes": stop.walk_minutes}
        backoff = self._backoff[index]
        try:
            if not backoff.ready():
                raise _Paused(f"Linz AG: Pause noch {backoff.blocked_for:.0f} s")
            payload = fetch_json(
                self._http,
                self.departure_url(stop),
                timeout=float(self._config.transit.timeout_seconds),
                label="Linz AG",
                max_bytes=MAX_BYTES,
            )
            departures = parse_departures(payload, stop.line, self._zone)
            if departures is None:
                raise FetchError("Linz AG: unerwartete Antwort")
            upcoming = select_upcoming(
                departures, now, stop.walk_minutes, self._config.transit.departures + SPARE_DEPARTURES
            )
            if not upcoming and len(_entries(payload) or []) >= DM_LIMIT:
                # The list ended before this line's next departure: that is
                # not "keine Abfahrt", it is not knowing.
                raise FetchError(f"Linz AG: Linie {stop.line} nicht unter den nächsten {DM_LIMIT}")
        except Exception as exc:  # noqa: BLE001 — one stop must never take the other down
            if isinstance(exc, FetchError):
                backoff.failure(exc)
            if not isinstance(exc, _Paused):
                logger.warning("transit %s %s: %s", stop.line, stop.label, exc)
            good = self._good.get(index)
            if good is not None and tick - good[1] <= STALE_MAX_AGE_SECONDS:
                return {**base, "status": "stale", "updated_at": good[2], "departures": good[0]}
            return {
                **base,
                "status": "error",
                "updated_at": good[2] if good else None,
                "departures": [],
            }
        backoff.success()
        stamp = _stamp(now)
        self._good[index] = (upcoming, tick, stamp)
        return {**base, "status": "ok", "updated_at": stamp, "departures": upcoming}

    def departure_url(self, stop: TransitStop) -> str:
        query = urlencode(
            {
                "outputFormat": "JSON",
                "language": "de",
                "type_dm": "stop",
                "name_dm": stop.stop_id,
                "mode": "direct",
                "useRealtime": "1",
                "limit": str(DM_LIMIT),
            }
        )
        return f"{self._config.transit.base_url}/XML_DM_REQUEST?{query}"


def parse_departures(payload: Any, line: str, zone: ZoneInfo) -> list[dict[str, Any]] | None:
    """The departures of ``line`` in an EFA ``XML_DM_REQUEST`` JSON answer,
    sorted by when they leave. ``None`` if it is not such an answer; ``[]``
    if it is one without this line (the EFA sends ``departureList: null``
    when nothing leaves)."""
    raw = _entries(payload)
    if raw is None:
        return None
    departures = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        serving = entry.get("servingLine")
        if not isinstance(serving, dict) or str(serving.get("number", "")).strip() != line:
            continue
        planned = _efa_time(entry.get("dateTime"), zone)
        if planned is None:
            continue
        expected = _efa_time(entry.get("realDateTime"), zone)
        direction = serving.get("direction")
        departures.append(
            {
                "planned": planned.isoformat(),
                "expected": expected.isoformat() if expected else None,
                "direction": direction.strip()[:48] if isinstance(direction, str) else "",
            }
        )
    departures.sort(key=_leaves)
    return departures


def _entries(payload: Any) -> list[Any] | None:
    """The raw ``departureList`` entries, ``None`` if it is not a DM answer."""
    if not isinstance(payload, dict) or "departureList" not in payload:
        return None
    raw = payload.get("departureList")
    # EFA JSON wraps a single entry as {"departure": {...}} instead of a list,
    # and sends null when nothing leaves.
    if isinstance(raw, dict):
        raw = raw.get("departure")
        raw = [raw] if isinstance(raw, dict) else raw
    if raw is None:
        return []
    return raw if isinstance(raw, list) else None


class _Paused(Exception):
    """The EFA asked for a pause (429/5xx); no request this round."""


def select_upcoming(
    departures: list[dict[str, Any]], now: datetime, walk_minutes: int, limit: int
) -> list[dict[str, Any]]:
    """Drop what has left (or cannot be reached on foot), keep ``limit``.
    app.js applies the same cut-off between two fetches."""
    cutoff = now + timedelta(minutes=walk_minutes) if walk_minutes > 0 else now - DEPARTED_GRACE
    return [departure for departure in departures if _leaves(departure) >= cutoff][:limit]


def _leaves(departure: dict[str, Any]) -> datetime:
    """Real time when the EFA has one, else the timetable."""
    return datetime.fromisoformat(departure["expected"] or departure["planned"])


def _efa_time(value: Any, zone: ZoneInfo) -> datetime | None:
    """An EFA ``dateTime`` block (strings, local wall time) → aware datetime."""
    if not isinstance(value, dict):
        return None
    try:
        local = datetime(
            int(value["year"]),
            int(value["month"]),
            int(value["day"]),
            int(value["hour"]),
            int(value["minute"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return local.replace(tzinfo=zone)


def _overall(stops: list[dict[str, Any]]) -> str:
    statuses = {stop["status"] for stop in stops}
    if statuses == {"ok"}:
        return "ok"
    if "ok" in statuses or "stale" in statuses:
        return "stale"
    return "error"


def _newest(stops: list[dict[str, Any]]) -> str | None:
    stamps = [stop["updated_at"] for stop in stops if stop["updated_at"]]
    return max(stamps) if stamps else None


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds")


def _envelope(status: str, data: dict[str, Any] | None, updated_at: str | None) -> dict[str, Any]:
    return {"status": status, "updated_at": updated_at, "message": "", "data": data}


# Demo mode without stops: two lines with a plausible morning, relative to now.
DEMO_STOPS: list[dict[str, Any]] = [
    {"stop_id": "60501350", "line": "70", "label": "Schiffswerft", "walk_minutes": 0,
     "offsets": [6, 26, 49, 69, 89]},
    {"stop_id": "60500097", "line": "46", "label": "Hafenportal", "walk_minutes": 0,
     "offsets": [12, 27, 42, 57, 72]},
]


def _demo_stops(now: datetime, zone: ZoneInfo) -> list[dict[str, Any]]:
    minute = now.astimezone(zone).replace(second=0, microsecond=0)
    stamp = _stamp(now)
    return [
        {
            "line": stop["line"],
            "label": stop["label"],
            "walk_minutes": 0,
            "status": "ok",
            "updated_at": stamp,
            "departures": [
                {"planned": (minute + timedelta(minutes=m)).isoformat(), "expected": None,
                 "direction": ""}
                for m in stop["offsets"]
            ],
        }
        for stop in DEMO_STOPS
    ]
