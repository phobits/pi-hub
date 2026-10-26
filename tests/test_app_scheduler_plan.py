from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from smart_display.app import _build_scheduled_jobs, _seed_unscheduled_provider_state
from smart_display.state_store import DETAIL_SECTIONS
from tests._support import make_app_config


def _noop() -> None:
    pass


def _job_names(config) -> list[str]:
    return [
        job.name
        for job in _build_scheduled_jobs(
            config,
            weather_refresh=_noop,
            calendar_refresh=_noop,
            spotify_refresh=_noop,
            screensaver_refresh=_noop,
            mock_refresh=_noop,
            detail_refresh=dict.fromkeys(DETAIL_SECTIONS, _noop),
        )
    ]


class SchedulerPlanTest(unittest.TestCase):
    """The Pi should only keep background threads for providers that have
    real periodic work. Disabled or incomplete providers are seeded once at
    boot and then stay quiet."""

    def test_disabled_default_providers_are_not_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir))

            self.assertEqual(_job_names(config), [])

    def test_enabled_weather_is_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir), weather_enabled=True)

            # #22: the detail sources follow the weather switch.
            self.assertEqual(
                _job_names(config),
                ["weather", "weather_warnings", "weather_hourly_geosphere", "weather_nowcast"],
            )

    def test_calendar_requires_complete_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir), calendar_enabled=True)

            self.assertNotIn("calendar", _job_names(config))

            configured = replace(
                config,
                calendar=replace(
                    config.calendar,
                    url="https://calendar.example.test",
                    username="user",
                    password="secret",
                ),
            )

            self.assertIn("calendar", _job_names(configured))

    def test_spotify_requires_complete_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir), spotify_enabled=True)

            self.assertNotIn("spotify", _job_names(config))

            configured = replace(
                config,
                spotify=replace(
                    config.spotify,
                    client_id="id",
                    client_secret="secret",
                    refresh_token="refresh",
                ),
            )

            self.assertIn("spotify", _job_names(configured))

    def test_screensaver_requires_source_url(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir))

            self.assertNotIn("screensaver", _job_names(config))

            configured = replace(
                config,
                screensaver=replace(
                    config.screensaver,
                    enabled=True,
                    source_url="https://lightroom.example.test/album",
                ),
            )

            self.assertIn("screensaver", _job_names(configured))

    def test_demo_mode_schedules_mock_only_for_disabled_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir))
            config = replace(config, app=replace(config.app, demo_mode=True))

            self.assertEqual(_job_names(config), ["mock"])

    def test_seed_runs_only_unscheduled_provider_refreshes(self) -> None:
        calls: list[str] = []

        def record(name: str):
            def _inner() -> None:
                calls.append(name)

            return _inner

        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir), weather_enabled=True)
            jobs = _build_scheduled_jobs(
                config,
                weather_refresh=record("weather"),
                calendar_refresh=record("calendar"),
                spotify_refresh=record("spotify"),
                screensaver_refresh=record("screensaver"),
                mock_refresh=record("mock"),
            )

            _seed_unscheduled_provider_state(
                jobs,
                weather_refresh=record("weather"),
                calendar_refresh=record("calendar"),
                spotify_refresh=record("spotify"),
                screensaver_refresh=record("screensaver"),
            )

            self.assertEqual(calls, ["calendar", "spotify", "screensaver"])


if __name__ == "__main__":
    unittest.main()


class FirstFetchRetryWiringTest(unittest.TestCase):
    """#19: the three provider jobs ask the store for an early retry; the
    screensaver and mock jobs have no first-fetch grace and do not."""

    def test_provider_jobs_ask_retry_wait_for_their_own_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(
                Path(temp_dir),
                weather_enabled=True,
                calendar_enabled=True,
                spotify_enabled=True,
                spotify_client_id="id",
                spotify_client_secret="secret",
                spotify_refresh_token="token",
            )
            config.calendar = replace(
                config.calendar, url="https://dav.example", username="u", password="p"
            )
            asked: list[str] = []
            jobs = _build_scheduled_jobs(
                config,
                weather_refresh=_noop,
                calendar_refresh=_noop,
                spotify_refresh=_noop,
                screensaver_refresh=_noop,
                mock_refresh=_noop,
                retry_wait=lambda section: asked.append(section) or 30.0,
            )
            by_name = {job.name: job for job in jobs}
            for name in ("weather", "calendar", "spotify"):
                self.assertEqual(by_name[name].retry_after(), 30.0)
            self.assertEqual(asked, ["weather", "calendar", "spotify"])
