from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from smart_display.config import load_config_from_mapping
from smart_display.models import (
    CalendarDaySection,
    CalendarEventItem,
    CalendarState,
    DASHBOARD_SCHEMA_VERSION,
    DashboardState,
    IncompatibleSchemaError,
    ProviderSnapshot,
    SpotifyState,
    SystemState,
    WeatherForecastItem,
    WeatherState,
)
from smart_display.state_store import StateStore


def _make_store(temp_dir: str) -> StateStore:
    config = load_config_from_mapping(
        {
            "app": {"data_dir": temp_dir, "locale": "de-AT", "timezone": "Europe/Vienna"},
            "screensaver": {"idle_timeout_seconds": 120, "image_duration_seconds": 15},
        },
        root_dir=Path(temp_dir),
    )
    return StateStore(config)


class StateStoreTest(unittest.TestCase):
    def test_to_dict_returns_detached_payload(self) -> None:
        # to_dict() no longer round-trips through from_dict(), so the thing worth
        # pinning is the NESTED containers: a shared forecast/section list would
        # hand /api/state callers a live alias into store state. A depth-2 scalar
        # cannot catch that — every to_dict() rebuilds its own dict literal.
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _make_store(temp_dir)
            store.update_section(
                "weather",
                WeatherState(
                    snapshot=ProviderSnapshot(status="ok"),
                    location_label="Linz",
                    forecast=[
                        WeatherForecastItem(
                            day_label="Morgen",
                            condition="Sonnig",
                            temperature_max_c=20.0,
                            temperature_min_c=10.0,
                        )
                    ],
                ),
            )
            store.update_section(
                "calendar",
                CalendarState(
                    snapshot=ProviderSnapshot(status="ok"),
                    sections=[
                        CalendarDaySection(
                            day_key="today",
                            section_date=date.today().isoformat(),
                            items=[
                                CalendarEventItem(
                                    title="Termin",
                                    starts_at="2026-07-18T09:00:00+02:00",
                                    ends_at=None,
                                    time_label="09:00",
                                )
                            ],
                        )
                    ],
                ),
            )

            payload = store.to_dict()
            payload["weather"]["location_label"] = "Manipuliert"
            payload["weather"]["forecast"].append({"day_label": "Eingeschmuggelt"})
            payload["weather"]["forecast"][0]["day_label"] = "Manipuliert"
            payload["calendar"]["sections"][0]["items"].clear()

            fresh = store.to_dict()
            self.assertEqual(fresh["weather"]["location_label"], "Linz")
            self.assertEqual(len(fresh["weather"]["forecast"]), 1)
            self.assertEqual(fresh["weather"]["forecast"][0]["day_label"], "Morgen")
            self.assertEqual(len(fresh["calendar"]["sections"][0]["items"]), 1)

    def test_error_keeps_last_known_good_weather(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _make_store(temp_dir)
            store.update_section(
                "weather",
                WeatherState(
                    snapshot=ProviderSnapshot(status="ok", updated_at="2026-04-08T09:00:00+00:00"),
                    location_label="Zuhause",
                    temperature_c=20.0,
                    condition="Klar",
                ),
            )
            store.mark_error(
                "weather",
                error_message="timeout",
                stale_after_seconds=600,
                source="open-meteo",
            )

            current = store.get_state().weather
            self.assertEqual(current.snapshot.status, "stale")
            self.assertEqual(current.temperature_c, 20.0)
            self.assertEqual(current.snapshot.error_message, "timeout")


class MarkErrorStatusMachineTest(unittest.TestCase):
    """#19: a failure without any good data is ``error`` — after the boot
    grace. Inside it the section stays ``empty`` ("wird geladen") so a Pi
    that starts before its WLAN does not flash an error. The ok → stale →
    error escalation is preserved."""

    def test_first_failure_without_data_waits_out_the_grace(self) -> None:
        from smart_display.state_store import FIRST_FETCH_GRACE_SECONDS
        from tests._support import FakeClock

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = FakeClock(monotonic=50.0)
            config = load_config_from_mapping(
                {"app": {"data_dir": temp_dir}}, root_dir=Path(temp_dir)
            )
            store = StateStore(config, monotonic=clock.monotonic)
            self.assertEqual(store.get_state().weather.snapshot.status, "empty")

            store.mark_error(
                "weather",
                error_message="temporarily unreachable",
                stale_after_seconds=600,
                source="open-meteo",
            )
            snapshot = store.get_state().weather.snapshot
            self.assertEqual(snapshot.status, "empty")
            self.assertEqual(snapshot.error_message, "temporarily unreachable")
            self.assertEqual(snapshot.source, "open-meteo")

            clock.advance(FIRST_FETCH_GRACE_SECONDS)
            store.mark_error(
                "weather", error_message="still down", stale_after_seconds=600,
                source="open-meteo",
            )
            snapshot = store.get_state().weather.snapshot
            self.assertEqual(snapshot.status, "error")
            self.assertIsNone(snapshot.updated_at)

    def test_mark_error_escalates_stale_to_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = _make_store(temp_dir)
            store.update_section(
                "weather",
                WeatherState(
                    snapshot=ProviderSnapshot(status="ok"),
                    location_label="Zuhause",
                    temperature_c=18.0,
                    condition="Wolkig",
                ),
            )
            store.mark_error(
                "weather", error_message="t1", stale_after_seconds=600, source="open-meteo"
            )
            self.assertEqual(store.get_state().weather.snapshot.status, "stale")

            store.mark_error(
                "weather", error_message="t2", stale_after_seconds=600, source="open-meteo"
            )
            self.assertEqual(store.get_state().weather.snapshot.status, "error")
            self.assertEqual(store.get_state().weather.snapshot.error_message, "t2")


def _realistic_dashboard_state() -> DashboardState:
    """Build a fully populated DashboardState so the round-trip test
    exercises every nested dataclass (weather forecast, calendar sections,
    spotify track, system metadata). Mirrors the shape a live Pi would
    serialise to ``last_good.json`` after an hour of uptime."""
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    return DashboardState(
        weather=WeatherState(
            snapshot=ProviderSnapshot(
                status="ok",
                updated_at="2026-04-08T09:00:00+00:00",
                source="open-meteo",
                error_message=None,
                stale_after_seconds=900,
            ),
            location_label="Zuhause",
            temperature_c=18.5,
            apparent_temperature_c=17.2,
            condition="Teilweise bewölkt",
            condition_code=2,
            forecast=[
                WeatherForecastItem(
                    day_label="Heute",
                    condition="Wolkig",
                    condition_code=2,
                    temperature_max_c=21.0,
                    temperature_min_c=11.3,
                ),
                WeatherForecastItem(
                    day_label="Morgen",
                    condition="Bedeckt",
                    condition_code=3,
                    temperature_max_c=19.5,
                    temperature_min_c=9.0,
                ),
            ],
        ),
        calendar=CalendarState(
            snapshot=ProviderSnapshot(status="ok", source="caldav"),
            items=[],
            sections=[
                CalendarDaySection(
                    day_key=today,
                    section_date=today,
                    items=[
                        CalendarEventItem(
                            title="Teamstandup",
                            starts_at=f"{today}T09:30:00+02:00",
                            ends_at=f"{today}T10:00:00+02:00",
                            time_label="09:30–10:00",
                            all_day=False,
                        )
                    ],
                ),
                CalendarDaySection(
                    day_key=tomorrow,
                    section_date=tomorrow,
                    items=[
                        CalendarEventItem(
                            title="Zahnarzt",
                            starts_at=f"{tomorrow}T14:15:00+02:00",
                            ends_at=f"{tomorrow}T15:00:00+02:00",
                            time_label="14:15–15:00",
                            all_day=False,
                        )
                    ],
                ),
            ],
        ),
        spotify=SpotifyState(
            snapshot=ProviderSnapshot(status="ok", source="spotify"),
            connected=True,
            is_playing=True,
            track_title="Weiß nicht",
            artist_name="Irgendwer",
            album_name="Album",
            album_art_url="https://example.com/cover.jpg",
            device_name="Wohnzimmer",
            device_type="Speaker",
            volume_percent=42,
            supports_volume=True,
            can_control=True,
            empty_message="",
        ),
        system=SystemState(
            generated_at="2026-04-08T09:00:05+00:00",
            locale="de-AT",
            timezone="Europe/Vienna",
            idle_timeout_seconds=120,
            screensaver_interval_seconds=15,
            screensaver_photo_count=12,
        ),
    )


class DashboardRoundTripTest(unittest.TestCase):
    """Any shape change that breaks round-trip is a migration blocker. The
    test serialises a fully-populated state, deserialises it, and asserts
    the JSON bytes are identical. A failure here means a future upgrade
    would either crash on load or lose data silently."""

    def test_round_trip_preserves_every_field(self) -> None:
        state = _realistic_dashboard_state()
        serialised = state.to_dict()
        # JSON round-trip via the same path StateStore uses.
        json_bytes = json.dumps(serialised, sort_keys=True).encode("utf-8")
        reloaded_payload = json.loads(json_bytes.decode("utf-8"))

        reloaded = DashboardState.from_dict(reloaded_payload)
        re_serialised = reloaded.to_dict()

        self.assertEqual(
            json.dumps(serialised, sort_keys=True),
            json.dumps(re_serialised, sort_keys=True),
        )
        # schema_version must be embedded so a future bump can detect drift.
        self.assertEqual(serialised["schema_version"], DASHBOARD_SCHEMA_VERSION)

    def test_missing_schema_version_still_loads(self) -> None:
        # Pre-versioning payloads (before V1 ships) must still deserialise
        # so the very first upgrade doesn't quarantine a valid cache.
        state = _realistic_dashboard_state()
        payload = state.to_dict()
        payload.pop("schema_version")
        reloaded = DashboardState.from_dict(payload)
        self.assertEqual(
            reloaded.weather.temperature_c, state.weather.temperature_c
        )

    def test_future_schema_version_raises(self) -> None:
        payload = _realistic_dashboard_state().to_dict()
        payload["schema_version"] = DASHBOARD_SCHEMA_VERSION + 99
        with self.assertRaises(IncompatibleSchemaError):
            DashboardState.from_dict(payload)


class SchemaMismatchQuarantineTest(unittest.TestCase):
    """If ``last_good.json`` was written by an incompatible version, the
    StateStore must quarantine it and boot from an empty state instead of
    crashing. Mirrors DiskCache's corrupt-JSON quarantine pattern."""

    def _make_config(self, temp_dir: str):
        return load_config_from_mapping(
            {
                "app": {
                    "data_dir": temp_dir,
                    "locale": "de-AT",
                    "timezone": "Europe/Vienna",
                },
                "screensaver": {
                    "idle_timeout_seconds": 120,
                    "image_duration_seconds": 15,
                },
            },
            root_dir=Path(temp_dir),
        )

    def test_incompatible_cache_is_quarantined_and_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._make_config(temp_dir)
            # Write a future-schema payload directly to the last_good path.
            stale_payload = _realistic_dashboard_state().to_dict()
            stale_payload["schema_version"] = DASHBOARD_SCHEMA_VERSION + 99
            config.last_good_path.parent.mkdir(parents=True, exist_ok=True)
            config.last_good_path.write_text(
                json.dumps(stale_payload), encoding="utf-8"
            )

            store = StateStore(config)

            # Fresh empty state — no fields carried over from the stale cache.
            current = store.get_state()
            self.assertEqual(current.weather.snapshot.status, "empty")
            self.assertIsNone(current.weather.temperature_c)

            # Original file renamed aside, new one re-created by the fresh boot.
            siblings = list(config.last_good_path.parent.iterdir())
            corrupt = [
                p for p in siblings if ".corrupt-" in p.name
            ]
            self.assertEqual(len(corrupt), 1)

    def test_malformed_payload_is_also_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self._make_config(temp_dir)
            config.last_good_path.parent.mkdir(parents=True, exist_ok=True)
            # Missing required nested shape entirely.
            config.last_good_path.write_text(
                json.dumps({"weather": "not a dict"}), encoding="utf-8"
            )

            store = StateStore(config)
            self.assertEqual(store.get_state().weather.snapshot.status, "empty")
            corrupt = [
                p
                for p in config.last_good_path.parent.iterdir()
                if ".corrupt-" in p.name
            ]
            self.assertEqual(len(corrupt), 1)


if __name__ == "__main__":
    unittest.main()


class PersistenceDebounceTest(unittest.TestCase):
    """Ticket #1: progress-only Spotify refreshes must not rewrite
    last_good.json; real changes always do; a deferred write lands on flush."""

    def _store_with_counter(self, temp_dir: str, clock):
        from smart_display.config import load_config_from_mapping

        config = load_config_from_mapping(
            {"app": {"data_dir": temp_dir}}, root_dir=Path(temp_dir)
        )
        store = StateStore(config, monotonic=clock.monotonic)
        writes = {"count": 0}
        original_save = store._cache.save

        def counting_save(payload):
            writes["count"] += 1
            original_save(payload)

        store._cache.save = counting_save  # type: ignore[method-assign]
        return store, writes

    def _playing(self, progress_ms: int) -> SpotifyState:
        return SpotifyState(
            snapshot=ProviderSnapshot(status="ok", updated_at=f"t{progress_ms}"),
            connected=True,
            is_playing=True,
            track_title="Song",
            artist_name="Band",
            can_control=True,
            progress_ms=progress_ms,
            duration_ms=240_000,
        )

    def test_progress_only_updates_are_debounced(self) -> None:
        from tests._support import FakeClock

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = FakeClock(monotonic=1000.0)
            store, writes = self._store_with_counter(temp_dir, clock)
            store.update_section("spotify", self._playing(0))
            self.assertEqual(writes["count"], 1)
            for step in range(1, 21):
                clock.advance(10)
                store.update_section("spotify", self._playing(step * 10_000))
            self.assertEqual(writes["count"], 1)
            # In-memory state is current regardless.
            self.assertEqual(store.get_state().spotify.progress_ms, 200_000)

    def test_real_change_writes_immediately(self) -> None:
        from tests._support import FakeClock

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = FakeClock(monotonic=1000.0)
            store, writes = self._store_with_counter(temp_dir, clock)
            store.update_section("spotify", self._playing(0))
            clock.advance(10)
            paused = self._playing(10_000)
            paused.is_playing = False
            store.update_section("spotify", paused)
            self.assertEqual(writes["count"], 2)
            store.mark_error("spotify", error_message="x", stale_after_seconds=60, source="spotify")
            self.assertEqual(writes["count"], 3)

    def test_deferred_write_lands_after_interval_or_flush(self) -> None:
        from smart_display.state_store import PERSIST_MIN_INTERVAL_SECONDS
        from tests._support import FakeClock

        with tempfile.TemporaryDirectory() as temp_dir:
            clock = FakeClock(monotonic=1000.0)
            store, writes = self._store_with_counter(temp_dir, clock)
            store.update_section("spotify", self._playing(0))
            clock.advance(30)
            store.update_section("spotify", self._playing(30_000))
            self.assertEqual(writes["count"], 1)
            store.flush()
            self.assertEqual(writes["count"], 2)
            store.flush()  # nothing pending
            self.assertEqual(writes["count"], 2)

            clock.advance(PERSIST_MIN_INTERVAL_SECONDS + 1)
            store.update_section("spotify", self._playing(60_000))
            self.assertEqual(writes["count"], 3)
            payload = json.loads(store.config.last_good_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["spotify"]["progress_ms"], 60_000)
