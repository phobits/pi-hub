from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from smart_display.cache.disk_cache import DiskCache
from smart_display.config import AppConfig
from smart_display.models import (
    CalendarState,
    DashboardState,
    IncompatibleSchemaError,
    ProviderSnapshot,
    SpotifyState,
    SystemState,
    TimerState,
    WeatherState,
    utcnow_iso,
)


logger = logging.getLogger(__name__)


# SD-card wear (Ticket #1): every mutation used to rewrite last_good.json. With
# Spotify playing, progress_ms changes on every 30 s refresh — ~2,900 writes a
# day for a value the UI interpolates anyway. Keys listed here never justify a
# write on their own; an otherwise identical payload is re-persisted at most
# once per PERSIST_MIN_INTERVAL_SECONDS so the on-disk copy never drifts far.
PERSIST_MIN_INTERVAL_SECONDS = 300.0
VOLATILE_KEYS = frozenset({"progress_ms", "generated_at", "updated_at"})

# #19: a failed first fetch is an error — but not in the first minutes after
# start, when WLAN or DNS may simply not be up yet. Until FIRST_FETCH_GRACE
# has passed, a section that has not had one good fetch *in this process*
# keeps the status it booted with (a fresh one stays ``empty``, "wird
# geladen"; one rehydrated from last_good.json stays as it was), and its job
# retries every FIRST_FETCH_RETRY instead of waiting its regular interval
# (15 min for weather). So the panel shows fresh data or an honest age/"nicht
# verfügbar" at most ~3.5 min after start, and a boot never flashes an error.
FIRST_FETCH_GRACE_SECONDS = 180.0
FIRST_FETCH_RETRY_SECONDS = 30.0

# #22: the weather detail sources, each its own section with its own snapshot
# (see DashboardState). They go through update_section/mark_error like the
# three home sections.
DETAIL_SECTIONS = (
    "weather_hourly_geosphere",
    "weather_hourly_openmeteo",
    "weather_nowcast",
    "weather_warnings",
)


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


class StateStore:
    def __init__(
        self,
        config: AppConfig,
        *,
        monotonic: Callable[[], float] | None = None,
    ):
        self.config = config
        self._lock = threading.Lock()
        self._cache = DiskCache(config.last_good_path)
        self._monotonic = monotonic or time.monotonic
        self._started_at = self._monotonic()
        # Sections the boot grace no longer covers (a good answer, or a
        # permanent failure, since start), and sections whose every attempt
        # since start failed — the grace is about these, not about the status
        # rehydrated from disk.
        self._answered: set[str] = set()
        self._missed: set[str] = set()
        self._last_persisted_key: Any = None
        self._last_persisted_at: float = float("-inf")
        self._dirty = False
        self._state = self._load_initial_state()
        self._provider_health: dict[str, ProviderSnapshot] = {
            "weather": self._state.weather.snapshot,
            "calendar": self._state.calendar.snapshot,
            "spotify": self._state.spotify.snapshot,
            **{name: getattr(self._state, name).snapshot for name in DETAIL_SECTIONS},
            "screensaver": ProviderSnapshot(status="empty", source="screensaver"),
        }

    def get_state(self) -> DashboardState:
        with self._lock:
            return DashboardState.from_dict(self._state.to_dict())

    def to_dict(self) -> dict[str, Any]:
        # ``DashboardState.to_dict`` already returns a detached tree of plain
        # dicts/lists. Re-hydrating a second DashboardState only to serialise it
        # again made every local /api/state poll do the same deep conversion
        # twice.
        with self._lock:
            return self._state.to_dict()

    def update_section(self, section_name: str, section: Any) -> None:
        with self._lock:
            setattr(self._state, section_name, section)
            self._answered.add(section_name)
            self._missed.discard(section_name)
            self._state.system.generated_at = utcnow_iso()
            self._provider_health[section_name] = replace(section.snapshot)
            self._persist_locked()

    def _in_grace_locked(self) -> bool:
        return self._monotonic() - self._started_at < FIRST_FETCH_GRACE_SECONDS

    def mark_error(
        self,
        section_name: str,
        *,
        error_message: str,
        stale_after_seconds: int,
        source: str,
        transient: bool = True,
    ) -> None:
        """Record a provider fetch failure.

        Status transitions:
          ``ok``    → ``stale`` (first failure after known-good data)
          ``stale`` → ``error``
          ``error`` → ``error``
          any       → ``error`` with ``transient=False``, see below
          ``empty``/``disabled`` → ``error`` (#19: the first fetch of a
                       configured source failed — never good data, so it must
                       not hide behind the calm "loading" state)

        Boot grace: until a section has had one good answer in this process,
        failures inside FIRST_FETCH_GRACE_SECONDS change nothing but the
        message (``disabled`` becomes ``empty``), and the job retries early —
        see ``retry_wait``. ``transient=False`` marks a failure no retry can
        fix (missing CalDAV dependency, Spotify refresh token rejected): it
        goes straight to
        ``error`` — cached good data included, so it cannot hide behind
        ``stale`` for an interval — and ends any early retry.

        ``updated_at`` stays the time of the last good fetch. Coming from
        ``empty``/``disabled`` there was none — a ``disabled`` snapshot (a
        source set up since the last run) carries the stamp of its "not set
        up" answer, so it is cleared rather than shown as a past success. The
        error message and source are refreshed so ops and /health see the
        latest attempt.
        """
        with self._lock:
            section = getattr(self._state, section_name)
            previous_status = section.snapshot.status
            never_good = previous_status in ("empty", "disabled")
            if not transient:
                # Ends the grace for this section for good: a later transient
                # failure must not restart the early retry.
                self._answered.add(section_name)
                self._missed.discard(section_name)
                next_status = "error"
            elif section_name not in self._answered and self._in_grace_locked():
                self._missed.add(section_name)
                next_status = "empty" if never_good else previous_status
            elif previous_status == "ok":
                next_status = "stale"
            else:
                next_status = "error"
            if never_good:
                section.snapshot.updated_at = None
            section.snapshot.status = next_status
            section.snapshot.error_message = error_message
            section.snapshot.stale_after_seconds = stale_after_seconds
            section.snapshot.source = source
            self._state.system.generated_at = utcnow_iso()
            self._provider_health[section_name] = replace(section.snapshot)
            self._persist_locked()

    def retry_wait(self, section_name: str) -> float | None:
        """Early retry while a section has only failed since start and the
        grace window is open; ``None`` = regular interval. The first failure
        after the grace applies the normal transitions, and the job falls back
        to its own cadence."""
        with self._lock:
            if (
                section_name in self._missed
                and section_name not in self._answered
                and self._in_grace_locked()
            ):
                return FIRST_FETCH_RETRY_SECONDS
            return None

    def set_provider_snapshot(self, name: str, snapshot: ProviderSnapshot) -> None:
        with self._lock:
            self._provider_health[name] = replace(snapshot)
            self._state.system.generated_at = utcnow_iso()
            self._persist_locked()

    def set_screensaver_photo_count(self, count: int) -> None:
        with self._lock:
            self._state.system.screensaver_photo_count = max(count, 0)
            self._state.system.generated_at = utcnow_iso()
            self._persist_locked()

    def get_timers(self) -> list[TimerState]:
        with self._lock:
            return [replace(timer) for timer in self._state.timers]

    def set_timers(self, timers: list[TimerState]) -> None:
        with self._lock:
            self._state.timers = [replace(timer) for timer in timers]
            self._state.system.generated_at = utcnow_iso()
            self._persist_locked()

    def flush(self) -> None:
        """Write a deferred payload to disk. Called on shutdown so a progress
        update skipped by the debounce is not lost across a restart."""
        with self._lock:
            if self._dirty:
                self._persist_locked(force=True)

    def health_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "generated_at": self._state.system.generated_at,
                "providers": {
                    name: snapshot.to_dict()
                    for name, snapshot in self._provider_health.items()
                },
            }

    def _persist_locked(self, *, force: bool = False) -> None:
        payload = self._state.to_dict()
        # The debounce key is the stripped tree itself: a dict comparison walks
        # the same nodes json.dumps would, without building a second string
        # of the whole state on every mutation (#4). DiskCache.save does the
        # one serialisation that is actually written.
        key = _strip_volatile(payload)
        now = self._monotonic()
        unchanged = key == self._last_persisted_key
        recent = now - self._last_persisted_at < PERSIST_MIN_INTERVAL_SECONDS
        if not force and unchanged and recent:
            self._dirty = True
            return
        self._cache.save(payload)
        self._last_persisted_key = key
        self._last_persisted_at = now
        self._dirty = False

    def _quarantine_cache(self) -> None:
        """Rename the on-disk cache aside so a bad payload can't poison
        the next boot. Mirrors ``DiskCache`` corruption handling so ops can
        always find the old file with a ``*.corrupt-*`` suffix."""
        path = self._cache.path
        try:
            if path.exists():
                target = path.with_name(
                    f"{path.name}.corrupt-{int(time.time())}"
                )
                os.replace(path, target)
                logger.warning("moved stale cache to %s", target)
        except OSError as exc:  # pragma: no cover — best effort
            logger.warning("could not quarantine %s: %s", path, exc)

    def _load_initial_state(self) -> DashboardState:
        payload = self._cache.load()
        if payload:
            try:
                state = DashboardState.from_dict(payload)
            except IncompatibleSchemaError as exc:
                # Same quarantine pattern as DiskCache (Plan S2): log once,
                # rename the file aside, fall through to an empty boot.
                logger.warning(
                    "quarantining last_good.json due to schema mismatch: %s", exc
                )
                self._quarantine_cache()
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                # ``from_dict`` duck-types its input — if the payload is a
                # list or a dict-of-string-at-expected-nested-dict, the
                # deserialiser raises AttributeError. Widen the catch so
                # any malformed cache lands in quarantine instead of
                # crashing the app at boot.
                logger.warning(
                    "quarantining last_good.json due to malformed payload: %s", exc
                )
                self._quarantine_cache()
            else:
                # #19: before v0.12 ``empty`` also meant "fetched, nothing in
                # it" and "not set up", both with an updated_at. Today it means
                # "never fetched", so a carried-over stamp would invent a past
                # success the moment the first fetch fails. The providers
                # re-report on their first tick.
                for name in DETAIL_SECTIONS + ("weather", "calendar", "spotify"):
                    section = getattr(state, name)
                    if section.snapshot.status == "empty":
                        section.snapshot.updated_at = None
                        section.snapshot.error_message = None
                state.system.locale = self.config.app.locale
                state.system.timezone = self.config.app.timezone
                state.system.idle_timeout_seconds = (
                    self.config.screensaver.idle_timeout_seconds
                )
                state.system.screensaver_interval_seconds = (
                    self.config.screensaver.image_duration_seconds
                )
                return state

        system = SystemState(
            locale=self.config.app.locale,
            timezone=self.config.app.timezone,
            idle_timeout_seconds=self.config.screensaver.idle_timeout_seconds,
            screensaver_interval_seconds=self.config.screensaver.image_duration_seconds,
        )
        return DashboardState(
            weather=WeatherState(
                snapshot=ProviderSnapshot(status="empty", source="weather")
            ),
            calendar=CalendarState(
                snapshot=ProviderSnapshot(status="empty", source="calendar")
            ),
            spotify=SpotifyState(
                snapshot=ProviderSnapshot(status="empty", source="spotify")
            ),
            system=system,
        )
