"""#19: "Keine Termine" only for a calendar that was fetched and is empty.

One matrix per provider over the states the UI has to tell apart: not set up,
never fetched, first fetch failed, fetched empty, fetched with data, later
failure with good data, and recovery with an empty answer. Plus a restart from
a v0.11 ``last_good.json``, where ``empty`` still meant three things at once.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from smart_display.http_client import HttpError
from smart_display.models import DASHBOARD_SCHEMA_VERSION
from smart_display.providers.caldav_provider import CalDAVProvider
from smart_display.providers.spotify_provider import SpotifyProvider
from smart_display.providers.weather_openmeteo import OpenMeteoProvider
from smart_display.state_store import (
    FIRST_FETCH_GRACE_SECONDS,
    FIRST_FETCH_RETRY_SECONDS,
    StateStore,
)
from tests._support import FakeClock, FakeHttpClient, make_app_config
from tests.test_calendar_formatting import _FakeCalendar, _FakeComponent, _FakeEvent


class _AuthError(Exception):
    """Stands in for caldav's AuthorizationError — the provider catches any
    exception, so the exact class does not matter."""


class _FakeDAV:
    """Scripted CalDAV server: each refresh pops one answer — a list of
    event titles for today, or an exception to raise."""

    def __init__(self) -> None:
        self.answers: list = []

    def module(self) -> types.ModuleType:
        dav = self

        class DAVClient:
            def __init__(self, **_kwargs) -> None:
                answer = dav.answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                self._titles = answer

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> None:
                return None

            def principal(self):
                titles = self._titles
                start = datetime.now(ZoneInfo("Europe/Vienna")).replace(
                    hour=23, minute=0, second=0, microsecond=0
                )
                events = [
                    _FakeEvent([_FakeComponent(summary=title, dtstart=start)])
                    for title in titles
                ]
                return types.SimpleNamespace(
                    calendars=lambda: [_FakeCalendar("Privat", events)]
                )

        fake = types.ModuleType("caldav")
        fake.DAVClient = DAVClient
        return fake


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock(monotonic=1000.0)

    def store_for(self, config) -> StateStore:
        return StateStore(config, monotonic=self.clock.monotonic)

    def past_grace(self) -> None:
        self.clock.advance(FIRST_FETCH_GRACE_SECONDS + 1)


class CalendarStatesTest(_Fixture):
    def _configured(self):
        config = make_app_config(self.tmp, calendar_enabled=True)
        config.calendar = replace(
            config.calendar, url="https://dav.example", username="u", password="p"
        )
        store = self.store_for(config)
        dav = _FakeDAV()
        patcher = mock.patch.dict(sys.modules, {"caldav": dav.module()})
        patcher.start()
        self.addCleanup(patcher.stop)
        return CalDAVProvider(config, store), store, dav

    def _calendar(self, store: StateStore):
        return store.get_state().calendar

    def test_disabled_source_is_disabled_not_an_error(self) -> None:
        config = make_app_config(self.tmp, calendar_enabled=False)
        store = self.store_for(config)
        CalDAVProvider(config, store).refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "disabled")
        self.assertIsNone(calendar.snapshot.error_message)

    def test_missing_credentials_count_as_not_set_up(self) -> None:
        # Enabled, but no URL/user/password — nothing to fetch, nothing failed.
        config = make_app_config(self.tmp, calendar_enabled=True)
        store = self.store_for(config)
        CalDAVProvider(config, store).refresh()
        self.assertEqual(self._calendar(store).snapshot.status, "disabled")

    def test_before_the_first_fetch_it_is_loading(self) -> None:
        _provider, store, _dav = self._configured()
        snapshot = self._calendar(store).snapshot
        self.assertEqual(snapshot.status, "empty")
        self.assertIsNone(snapshot.updated_at)

    def test_failed_first_fetch_waits_out_the_boot_grace(self) -> None:
        # WLAN may not be up yet: stay "loading", retry early, no error.
        provider, store, dav = self._configured()
        dav.answers = [OSError("Name or service not known")]
        provider.refresh()
        snapshot = self._calendar(store).snapshot
        self.assertEqual(snapshot.status, "empty")
        self.assertEqual(store.retry_wait("calendar"), FIRST_FETCH_RETRY_SECONDS)

    def test_network_up_within_grace_shows_data_without_an_error(self) -> None:
        provider, store, dav = self._configured()
        dav.answers = [OSError("no route"), ["Zahnarzt"]]
        provider.refresh()
        self.clock.advance(FIRST_FETCH_RETRY_SECONDS)
        provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "ok")
        self.assertIsNone(store.retry_wait("calendar"))

    def test_wrong_credentials_without_cache_is_an_error(self) -> None:
        provider, store, dav = self._configured()
        dav.answers = [_AuthError("401 Unauthorized"), _AuthError("401 Unauthorized")]
        provider.refresh()
        self.past_grace()
        provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "error")
        # Never had good data — no stamp, nothing that could read as "Stand".
        self.assertIsNone(calendar.snapshot.updated_at)
        self.assertEqual(calendar.sections, [])
        # Back on the regular interval once the error is shown.
        self.assertIsNone(store.retry_wait("calendar"))

    def test_successful_empty_fetch_is_ok_without_items(self) -> None:
        provider, store, dav = self._configured()
        dav.answers = [[]]
        provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "ok")
        self.assertIsNotNone(calendar.snapshot.updated_at)
        self.assertEqual(calendar.sections, [])

    def test_filled_calendar_is_ok_with_items(self) -> None:
        provider, store, dav = self._configured()
        dav.answers = [["Zahnarzt"]]
        provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "ok")
        self.assertEqual([item.title for item in calendar.items], ["Zahnarzt"])

    def test_later_failure_keeps_good_events_and_their_age(self) -> None:
        provider, store, dav = self._configured()
        dav.answers = [["Zahnarzt"], OSError("timeout"), OSError("timeout")]
        provider.refresh()
        good_at = self._calendar(store).snapshot.updated_at
        provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "stale")
        self.assertEqual(calendar.snapshot.updated_at, good_at)
        self.assertEqual([item.title for item in calendar.items], ["Zahnarzt"])
        provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "error")
        self.assertEqual(calendar.snapshot.updated_at, good_at)
        self.assertEqual(len(calendar.sections), 1)

    def test_failure_after_an_empty_success_still_goes_stale(self) -> None:
        # The case that never got a hint before: empty → (fail) → empty.
        provider, store, dav = self._configured()
        dav.answers = [[], OSError("timeout")]
        provider.refresh()
        provider.refresh()
        snapshot = self._calendar(store).snapshot
        self.assertEqual(snapshot.status, "stale")
        self.assertIsNotNone(snapshot.updated_at)

    def test_recovery_with_an_empty_answer_replaces_old_events(self) -> None:
        provider, store, dav = self._configured()
        dav.answers = [["Zahnarzt"], OSError("timeout"), OSError("timeout"), []]
        for _ in range(4):
            provider.refresh()
        calendar = self._calendar(store)
        self.assertEqual(calendar.snapshot.status, "ok")
        self.assertEqual(calendar.items, [])
        self.assertEqual(calendar.sections, [])

    def test_calendar_failure_leaves_other_sections_alone(self) -> None:
        provider, store, dav = self._configured()
        before = store.to_dict()
        dav.answers = [_AuthError("401")]
        self.past_grace()
        provider.refresh()
        after = store.to_dict()
        for name in ("weather", "spotify", "timers"):
            self.assertEqual(after[name], before[name], name)


class WeatherStatesTest(_Fixture):
    def test_disabled_weather_is_disabled(self) -> None:
        config = make_app_config(self.tmp, weather_enabled=False)
        store = self.store_for(config)
        OpenMeteoProvider(config, store, http_client=FakeHttpClient()).refresh()
        weather = store.get_state().weather
        self.assertEqual(weather.snapshot.status, "disabled")
        # The UI owns the wording; the provider no longer fakes a condition.
        self.assertEqual(weather.condition, "")

    def test_failed_first_fetch_is_an_error_not_endless_loading(self) -> None:
        config = make_app_config(self.tmp, weather_enabled=True)
        store = self.store_for(config)
        http = FakeHttpClient()
        provider = OpenMeteoProvider(config, store, http_client=http)
        http.add_error("GET", _any_url(http), HttpError("connection refused"))
        self.past_grace()
        provider.refresh()
        snapshot = store.get_state().weather.snapshot
        self.assertEqual(snapshot.status, "error")
        self.assertIsNone(snapshot.updated_at)


class SpotifyStatesTest(_Fixture):
    def _provider(self, *, configured: bool):
        secrets = (
            {"spotify_client_id": "id", "spotify_client_secret": "s", "spotify_refresh_token": "r"}
            if configured
            else {}
        )
        config = make_app_config(self.tmp, spotify_enabled=configured, **secrets)
        store = self.store_for(config)
        http = FakeHttpClient()
        provider = SpotifyProvider(config, store, http_client=http)
        provider._access_token = "seeded"
        provider._token_expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
        return provider, store, http

    def test_not_configured_is_disabled(self) -> None:
        provider, store, _http = self._provider(configured=False)
        provider.refresh()
        spotify = store.get_state().spotify
        self.assertEqual(spotify.snapshot.status, "disabled")
        self.assertFalse(spotify.connected)

    def test_nothing_playing_is_a_successful_fetch(self) -> None:
        provider, store, http = self._provider(configured=True)
        http.add_response("GET", PLAYER_URL, status=204, body=b"")
        provider.refresh()
        spotify = store.get_state().spotify
        self.assertEqual(spotify.snapshot.status, "ok")
        self.assertTrue(spotify.connected)

    def test_failed_first_fetch_is_an_error_not_hidden(self) -> None:
        # Before #19 this stayed ``empty`` + not connected, i.e. the music bar
        # vanished without a word. ``error`` gives it the 60 px error shape.
        provider, store, http = self._provider(configured=True)
        http.add_response("GET", PLAYER_URL, status=503, body={"error": "unavailable"})
        self.past_grace()
        provider.refresh()
        snapshot = store.get_state().spotify.snapshot
        self.assertEqual(snapshot.status, "error")
        self.assertIsNone(snapshot.updated_at)

    def test_error_status_without_a_body_is_an_error_not_silence(self) -> None:
        # A 503/401 with an empty body used to read as "Keine aktive Wiedergabe".
        for status in (503, 401):
            with self.subTest(status=status):
                provider, store, http = self._provider(configured=True)
                http.add_response("GET", PLAYER_URL, status=status, body=b"")
                self.past_grace()
                provider.refresh()
                self.assertEqual(store.get_state().spotify.snapshot.status, "error")

    def test_empty_bodied_error_gets_the_german_status_message(self) -> None:
        provider, store, http = self._provider(configured=True)
        http.add_response("GET", PLAYER_URL, status=503, body=b"")
        self.past_grace()
        provider.refresh()
        self.assertEqual(store.get_state().spotify.snapshot.error_message,
                         "Spotify nicht erreichbar.")

    def test_redirect_without_a_body_is_an_error(self) -> None:
        # The HTTP client follows no redirects; a 3xx is not "nothing playing".
        provider, store, http = self._provider(configured=True)
        http.add_response("GET", PLAYER_URL, status=302, body=b"")
        self.past_grace()
        provider.refresh()
        self.assertEqual(store.get_state().spotify.snapshot.status, "error")

    def test_rejected_refresh_token_skips_the_boot_grace(self) -> None:
        provider, store, http = self._provider(configured=True)
        provider._access_token = None
        http.add_response("POST", TOKEN_URL, status=400,
                          body={"error": "invalid_grant", "error_description": "revoked"})
        provider.refresh()
        snapshot = store.get_state().spotify.snapshot
        self.assertEqual(snapshot.status, "error")
        # German, not the endpoint's raw JSON.
        self.assertEqual(snapshot.error_message, "Spotify-Anmeldung abgelaufen.")
        self.assertIsNone(store.retry_wait("spotify"))
        # A later outage of the endpoint does not restart the early retry.
        http.add_response("POST", TOKEN_URL, status=503, body=b"")
        provider.refresh()
        self.assertIsNone(store.retry_wait("spotify"))

    def test_400_without_an_oauth_code_stays_transient(self) -> None:
        # A proxy or captive portal answering 400 is not a revoked token.
        provider, store, http = self._provider(configured=True)
        provider._access_token = None
        http.add_response("POST", TOKEN_URL, status=400, body=b"<html>Login</html>")
        provider.refresh()
        self.assertEqual(store.get_state().spotify.snapshot.status, "empty")
        self.assertEqual(store.retry_wait("spotify"), FIRST_FETCH_RETRY_SECONDS)

    def test_success_with_a_non_object_body_is_an_error(self) -> None:
        provider, store, http = self._provider(configured=True)
        http.add_response("GET", PLAYER_URL, status=200, body=b"null")
        self.past_grace()
        provider.refresh()
        self.assertEqual(store.get_state().spotify.snapshot.status, "error")

    def test_token_endpoint_outage_waits_out_the_boot_grace(self) -> None:
        provider, store, http = self._provider(configured=True)
        provider._access_token = None
        http.add_response("POST", TOKEN_URL, status=503, body=b"")
        provider.refresh()
        self.assertEqual(store.get_state().spotify.snapshot.status, "empty")
        self.assertEqual(store.retry_wait("spotify"), FIRST_FETCH_RETRY_SECONDS)

    def test_success_without_a_body_is_still_nothing_playing(self) -> None:
        provider, store, http = self._provider(configured=True)
        http.add_response("GET", PLAYER_URL, status=200, body=b"")
        provider.refresh()
        spotify = store.get_state().spotify
        self.assertEqual(spotify.snapshot.status, "ok")
        self.assertTrue(spotify.connected)


class PermanentFailureTest(_Fixture):
    """``transient=False``: a failure no retry heals shows at once, through
    the real provider paths that raise it."""

    def _calendar_store(self, cached_ok: bool = False) -> tuple[CalDAVProvider, StateStore]:
        config = make_app_config(self.tmp, calendar_enabled=True)
        config.calendar = replace(
            config.calendar, url="https://dav.example", username="u", password="p"
        )
        if cached_ok:
            payload = StateStore(config).to_dict()
            payload["calendar"]["snapshot"].update(
                {"status": "ok", "updated_at": "2026-10-24T20:10:00+00:00"}
            )
            config.last_good_path.write_text(json.dumps(payload), encoding="utf-8")
        store = self.store_for(config)
        # None in sys.modules makes ``import caldav`` raise ImportError.
        patcher = mock.patch.dict(sys.modules, {"caldav": None})
        patcher.start()
        self.addCleanup(patcher.stop)
        return CalDAVProvider(config, store), store

    def test_missing_caldav_dependency_skips_the_boot_grace(self) -> None:
        provider, store = self._calendar_store()
        provider.refresh()
        snapshot = store.get_state().calendar.snapshot
        self.assertEqual(snapshot.status, "error")
        self.assertIn("CalDAV dependencies fehlen", snapshot.error_message)
        self.assertIsNone(store.retry_wait("calendar"))

    def test_permanent_failure_does_not_hide_behind_cached_data(self) -> None:
        provider, store = self._calendar_store(cached_ok=True)
        provider.refresh()
        snapshot = store.get_state().calendar.snapshot
        self.assertEqual(snapshot.status, "error")
        self.assertEqual(snapshot.updated_at, "2026-10-24T20:10:00+00:00")

    def test_permanent_failure_ends_an_early_retry_already_running(self) -> None:
        store = self.store_for(make_app_config(self.tmp))
        store.mark_error("calendar", error_message="no route", stale_after_seconds=600,
                         source="caldav")
        self.assertEqual(store.retry_wait("calendar"), FIRST_FETCH_RETRY_SECONDS)
        store.mark_error("calendar", error_message="CalDAV dependencies fehlen",
                         stale_after_seconds=600, source="caldav", transient=False)
        self.assertEqual(store.get_state().calendar.snapshot.status, "error")
        self.assertIsNone(store.retry_wait("calendar"))


PLAYER_URL = "https://api.spotify.com/v1/me/player?additional_types=episode"
TOKEN_URL = "https://accounts.spotify.com/api/token"


def _any_url(http: FakeHttpClient) -> str:
    """The Open-Meteo URL the provider builds, without re-deriving its query."""
    with tempfile.TemporaryDirectory() as tmp:
        config = make_app_config(Path(tmp), weather_enabled=True)
        return OpenMeteoProvider(config, None).request_url()  # type: ignore[arg-type]


class LegacyCacheRestartTest(_Fixture):
    """A v0.11 ``last_good.json``: ``empty`` there could mean "fetched and
    empty" or "not set up", both with an ``updated_at``."""

    def _write_v011_cache(self, config) -> None:
        payload = {
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "weather": {
                "snapshot": {"status": "ok", "updated_at": "2026-10-24T08:00:00+00:00",
                             "stale_after_seconds": 1800, "error_message": None,
                             "source": "open-meteo"},
                "location_label": "Zuhause", "temperature_c": 12.0,
                "apparent_temperature_c": 11.0, "condition": "Klar",
                "condition_code": 0, "forecast": [],
            },
            "calendar": {
                "snapshot": {"status": "empty", "updated_at": "2026-10-24T08:00:00+00:00",
                             "stale_after_seconds": 600, "error_message": None,
                             "source": "caldav"},
                "items": [], "sections": [],
                "empty_message": "Keine Termine in den nächsten drei Tagen.",
            },
            "spotify": {
                "snapshot": {"status": "empty", "updated_at": "2026-10-24T08:00:00+00:00",
                             "stale_after_seconds": 60, "error_message": None,
                             "source": "spotify"},
                "connected": True, "empty_message": "Keine aktive Wiedergabe.",
            },
            "system": {"locale": "de-AT", "timezone": "Europe/Vienna",
                       "idle_timeout_seconds": 120, "screensaver_interval_seconds": 15,
                       "generated_at": "2026-10-24T08:00:00+00:00",
                       "screensaver_photo_count": 3},
            "timers": [],
        }
        config.last_good_path.parent.mkdir(parents=True, exist_ok=True)
        config.last_good_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_old_cache_loads_without_quarantine(self) -> None:
        config = make_app_config(self.tmp)
        self._write_v011_cache(config)
        state = self.store_for(config).get_state()
        self.assertEqual(state.weather.snapshot.status, "ok")
        self.assertEqual(state.weather.temperature_c, 12.0)
        self.assertTrue(state.spotify.connected)
        siblings = [p.name for p in config.last_good_path.parent.iterdir()]
        self.assertFalse(any(".corrupt-" in name for name in siblings), siblings)

    def test_old_empty_does_not_invent_a_past_success(self) -> None:
        config = make_app_config(self.tmp)
        self._write_v011_cache(config)
        store = self.store_for(config)
        calendar = store.get_state().calendar
        self.assertEqual(calendar.snapshot.status, "empty")
        self.assertIsNone(calendar.snapshot.updated_at)
        # Good data elsewhere keeps its age.
        self.assertEqual(
            store.get_state().weather.snapshot.updated_at, "2026-10-24T08:00:00+00:00"
        )
        # First fetch after the upgrade fails: an error with no "Stand".
        self.past_grace()
        store.mark_error("calendar", error_message="401", stale_after_seconds=600,
                         source="caldav")
        snapshot = store.get_state().calendar.snapshot
        self.assertEqual(snapshot.status, "error")
        self.assertIsNone(snapshot.updated_at)


class BootGraceWithCacheTest(_Fixture):
    """The usual boot has a last_good.json, so sections come back ``ok`` —
    the grace must hold for them too, not only for a first-ever boot."""

    def _cached_store(self, calendar_snapshot: dict) -> StateStore:
        config = make_app_config(self.tmp)
        payload = StateStore(config).to_dict()
        payload["calendar"]["snapshot"].update(calendar_snapshot)
        config.last_good_path.write_text(json.dumps(payload), encoding="utf-8")
        return self.store_for(config)

    def _fail(self, store: StateStore) -> None:
        store.mark_error("calendar", error_message="no route", stale_after_seconds=600,
                         source="caldav")

    def test_cached_ok_survives_a_failure_inside_the_grace_and_retries_early(self) -> None:
        store = self._cached_store({"status": "ok", "updated_at": "2026-10-24T20:10:00+00:00"})
        self._fail(store)
        self.assertEqual(store.get_state().calendar.snapshot.status, "ok")
        self.assertEqual(store.retry_wait("calendar"), FIRST_FETCH_RETRY_SECONDS)
        # Network up 30 s later: a fresh answer, back to the regular interval.
        store.update_section("calendar", replace(store.get_state().calendar))
        self.assertIsNone(store.retry_wait("calendar"))

    def test_cached_ok_goes_stale_once_the_grace_is_over(self) -> None:
        store = self._cached_store({"status": "ok", "updated_at": "2026-10-24T20:10:00+00:00"})
        self._fail(store)
        self.past_grace()
        self._fail(store)
        snapshot = store.get_state().calendar.snapshot
        self.assertEqual(snapshot.status, "stale")
        self.assertEqual(snapshot.updated_at, "2026-10-24T20:10:00+00:00")
        self.assertIsNone(store.retry_wait("calendar"))

    def test_no_early_retry_after_a_good_answer(self) -> None:
        # Grace is about the boot, not about every later hiccup.
        store = self._cached_store({"status": "ok", "updated_at": "2026-10-24T20:10:00+00:00"})
        store.update_section("calendar", replace(store.get_state().calendar))
        self._fail(store)
        self.assertEqual(store.get_state().calendar.snapshot.status, "stale")
        self.assertIsNone(store.retry_wait("calendar"))

    def test_source_set_up_since_last_run_does_not_inherit_a_stamp(self) -> None:
        # last_good.json still says "not set up" (with the stamp of that answer);
        # now it is configured and the first fetch fails.
        store = self._cached_store({"status": "disabled", "updated_at": "2026-10-24T08:00:00+00:00"})
        self._fail(store)
        snapshot = store.get_state().calendar.snapshot
        self.assertEqual(snapshot.status, "empty")
        self.assertIsNone(snapshot.updated_at)
        self.past_grace()
        self._fail(store)
        snapshot = store.get_state().calendar.snapshot
        self.assertEqual(snapshot.status, "error")
        self.assertIsNone(snapshot.updated_at)


if __name__ == "__main__":
    unittest.main()
