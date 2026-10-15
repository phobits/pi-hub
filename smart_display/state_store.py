from __future__ import annotations

import json
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
        self._last_persisted_key: str | None = None
        self._last_persisted_at: float = float("-inf")
        self._dirty = False
        self._state = self._load_initial_state()
        self._provider_health: dict[str, ProviderSnapshot] = {
            "weather": self._state.weather.snapshot,
            "calendar": self._state.calendar.snapshot,
            "spotify": self._state.spotify.snapshot,
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

    def update_section(
        self, section_name: str, section: WeatherState | CalendarState | SpotifyState
    ) -> None:
        with self._lock:
            setattr(self._state, section_name, section)
            self._state.system.generated_at = utcnow_iso()
            self._provider_health[section_name] = replace(section.snapshot)
            self._persist_locked()

    def mark_error(
        self,
        section_name: str,
        *,
        error_message: str,
        stale_after_seconds: int,
        source: str,
    ) -> None:
        """Record a provider fetch failure.

        Status transitions (Plan B4):
          ``ok``    → ``stale`` (first failure after known-good data)
          ``stale`` → ``error``
          ``error`` → ``error``
          ``empty`` → ``empty`` (no prior data — the UI keeps showing "Leer"
                       rather than flipping to "Fehler" the moment a not-yet-
                       configured provider is first polled)

        In every case the error message and source are refreshed so ops and
        the /health endpoint can see the latest attempt.
        """
        with self._lock:
            section = getattr(self._state, section_name)
            previous_status = section.snapshot.status
            if previous_status == "empty":
                next_status = "empty"
            elif previous_status == "ok":
                next_status = "stale"
            else:
                next_status = "error"
            section.snapshot.status = next_status
            section.snapshot.error_message = error_message
            section.snapshot.stale_after_seconds = stale_after_seconds
            section.snapshot.source = source
            self._state.system.generated_at = utcnow_iso()
            self._provider_health[section_name] = replace(section.snapshot)
            self._persist_locked()

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
        key = json.dumps(
            _strip_volatile(payload), sort_keys=True, separators=(",", ":")
        )
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
