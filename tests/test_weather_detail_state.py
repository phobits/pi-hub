"""#22: what reaches the weather screen — one hourly series, warnings with a
bounded visibility, persistence across a restart, the poll payload, the
scheduler plan, the shared icon contract and the solar times."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from smart_display.app import _build_scheduled_jobs
from smart_display.models import (
    DashboardState,
    HourlyForecastState,
    HourlyPoint,
    ProviderSnapshot,
    WarningsState,
    WeatherWarning,
)
from smart_display.state_store import DETAIL_SECTIONS, StateStore
from smart_display.sun import is_daylight, sun_times
from smart_display.weather_codes import geosphere_condition, warning_title, wmo_condition
from smart_display.weather_detail import (
    UNBOUNDED_WARNING_HOLD,
    prepare_warnings,
    screensaver_warnings,
    select_hourly,
)
from tests._support import make_app_config, make_blueprint_app

NOW = datetime(2026, 10, 26, 9, 22, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "weather"


def iso(moment: datetime) -> str:
    return moment.isoformat()


def series(status: str, source: str, hours: list[int] | None) -> dict:
    start = NOW.replace(minute=0)
    points = [
        HourlyPoint(time=iso(start + timedelta(hours=h)), temperature_c=10.0 + h).to_dict()
        for h in (hours or [])
    ]
    return HourlyForecastState(
        snapshot=ProviderSnapshot(status=status, source=source, updated_at=iso(NOW)),
        points=[HourlyPoint.from_dict(p) for p in points],
    ).to_dict()


def state_with(geo: dict, om: dict) -> dict:
    return {"weather_hourly_geosphere": geo, "weather_hourly_openmeteo": om}


class SelectHourlyTest(unittest.TestCase):
    def test_geosphere_wins_when_current(self) -> None:
        picked = select_hourly(state_with(series("ok", "geosphere-claef", [0, 1, 2]), series("ok", "open-meteo", [0, 1])), NOW)
        self.assertEqual(picked["snapshot"]["source"], "geosphere-claef")
        self.assertFalse(picked["fallback"])

    def test_stale_geosphere_with_future_hours_still_wins(self) -> None:
        picked = select_hourly(state_with(series("stale", "geosphere-claef", [1, 2]), series("ok", "open-meteo", [1])), NOW)
        self.assertEqual(picked["snapshot"]["source"], "geosphere-claef")

    def test_whole_series_switches_to_open_meteo_never_mixed(self) -> None:
        picked = select_hourly(state_with(series("error", "geosphere-claef", []), series("ok", "open-meteo", [0, 1, 2])), NOW)
        self.assertEqual(picked["snapshot"]["source"], "open-meteo")
        self.assertTrue(picked["fallback"])
        self.assertEqual(len(picked["points"]), 3)

    def test_geosphere_with_only_past_hours_falls_back(self) -> None:
        picked = select_hourly(state_with(series("stale", "geosphere-claef", [-3, -2, 0]), series("ok", "open-meteo", [1])), NOW)
        self.assertEqual(picked["snapshot"]["source"], "open-meteo")

    def test_geosphere_still_loading_is_not_reported_as_an_outage(self) -> None:
        picked = select_hourly(state_with(series("empty", "geosphere-claef", []), series("ok", "open-meteo", [0, 1])), NOW)
        self.assertEqual(picked["snapshot"]["source"], "open-meteo")
        self.assertFalse(picked["fallback"])

    def test_disabled_geosphere_is_not_a_fallback(self) -> None:
        picked = select_hourly(state_with(series("disabled", "geosphere-claef", None), series("ok", "open-meteo", [1])), NOW)
        self.assertEqual(picked["snapshot"]["source"], "open-meteo")
        self.assertFalse(picked["fallback"])

    def test_nothing_usable_reports_the_primary_state(self) -> None:
        picked = select_hourly(state_with(series("empty", "geosphere-claef", None), series("error", "open-meteo", None)), NOW)
        self.assertEqual(picked["snapshot"]["status"], "empty")
        self.assertEqual(picked["points"], [])


def warning(wid: str, *, level=2, wtype=1, begins: datetime | None, ends: datetime | None) -> dict:
    return WeatherWarning(
        id=wid,
        type=wtype,
        level=level,
        begins_at=iso(begins) if begins else None,
        ends_at=iso(ends) if ends else None,
    ).to_dict()


def warnings_section(*items: dict, status="ok", fetched=NOW) -> dict:
    return WarningsState(
        snapshot=ProviderSnapshot(status=status, updated_at=iso(fetched) if fetched else None),
        items=[WeatherWarning.from_dict(i) for i in items],
    ).to_dict()


class WarningRulesTest(unittest.TestCase):
    def test_sorted_by_level_then_begin(self) -> None:
        h = timedelta(hours=1)
        section = warnings_section(
            warning("a", level=1, begins=NOW - h, ends=NOW + 5 * h),
            warning("b", level=3, begins=NOW + 2 * h, ends=NOW + 6 * h),
            warning("c", level=2, begins=NOW + h, ends=NOW + 6 * h),
            warning("d", level=3, begins=NOW - h, ends=NOW + 6 * h),
        )
        self.assertEqual([i["id"] for i in prepare_warnings(section, NOW)["items"]], ["d", "b", "c", "a"])

    def test_each_warning_carries_its_display_name(self) -> None:
        section = warnings_section(warning("a", level=2, wtype=1, begins=NOW, ends=NOW + timedelta(hours=1)))
        self.assertEqual(prepare_warnings(section, NOW)["items"][0]["title"], "Orange Windwarnung")

    def test_display_names_never_guess(self) -> None:
        self.assertEqual(warning_title(3, 1), "Gelbe Schneewarnung")
        self.assertEqual(warning_title(7, 3), "Rote Kältewarnung")
        self.assertEqual(warning_title(9, 2), "Orange Wetterwarnung")
        self.assertEqual(warning_title(5, None), "Gewitterwarnung")
        self.assertEqual(warning_title(None, None), "Wetterwarnung")
        self.assertEqual(warning_title(True, True), "Wetterwarnung", "a bool is not a code")

    def test_valid_end_is_the_visibility_end_and_exclusive(self) -> None:
        ends = NOW + timedelta(hours=2)
        section = warnings_section(warning("a", begins=NOW, ends=ends))
        item = prepare_warnings(section, NOW)["items"][0]
        self.assertEqual(item["visible_until"], iso(ends))
        self.assertEqual(prepare_warnings(section, ends)["items"], [], "end is exclusive")

    def test_expiry_needs_no_new_fetch(self) -> None:
        section = warnings_section(warning("a", begins=NOW - timedelta(hours=1), ends=NOW + timedelta(minutes=30)))
        later = NOW + timedelta(minutes=31)
        self.assertEqual(len(screensaver_warnings(section, NOW)), 1)
        self.assertEqual(screensaver_warnings(section, later), [])

    def test_missing_end_is_held_only_for_a_bounded_time(self) -> None:
        fetched = NOW - timedelta(hours=1)
        section = warnings_section(warning("a", begins=NOW - timedelta(hours=2), ends=None), status="stale", fetched=fetched)
        item = prepare_warnings(section, NOW)["items"][0]
        self.assertEqual(item["visible_until"], iso(fetched + UNBOUNDED_WARNING_HOLD))
        self.assertEqual(prepare_warnings(section, fetched + UNBOUNDED_WARNING_HOLD)["items"], [])

    def test_inverted_window_uses_the_bounded_rule(self) -> None:
        section = warnings_section(warning("a", begins=NOW + timedelta(hours=1), ends=NOW))
        item = prepare_warnings(section, NOW)["items"][0]
        self.assertEqual(item["visible_until"], iso(NOW + UNBOUNDED_WARNING_HOLD))

    def test_no_end_and_never_fetched_is_never_shown(self) -> None:
        section = warnings_section(warning("a", begins=NOW, ends=None), status="error", fetched=None)
        self.assertEqual(prepare_warnings(section, NOW)["items"], [])

    def test_screensaver_takes_active_and_next_24_hours_only(self) -> None:
        h = timedelta(hours=1)
        section = warnings_section(
            warning("active", begins=NOW - h, ends=NOW + h),
            warning("soon", begins=NOW + 23 * h, ends=NOW + 30 * h),
            warning("later", begins=NOW + 24 * h, ends=NOW + 30 * h),
            warning("no-begin", begins=None, ends=NOW + 2 * h),
        )
        self.assertEqual({i["id"] for i in screensaver_warnings(section, NOW)}, {"active", "soon", "no-begin"})

    def test_stale_warnings_stay_until_their_end(self) -> None:
        section = warnings_section(
            warning("a", begins=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=4)),
            status="error",
            fetched=NOW - timedelta(hours=3),
        )
        self.assertEqual(len(screensaver_warnings(section, NOW)), 1)


class PersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.config = make_app_config(self.tmp, weather_enabled=True)

    def test_detail_sections_survive_a_restart(self) -> None:
        store = StateStore(self.config)
        hourly = HourlyForecastState.from_dict(series("ok", "geosphere-claef", [1, 2]))
        hourly.model_run = "2026-10-26T03:00:00+00:00"
        store.update_section("weather_hourly_geosphere", hourly)
        store.update_section(
            "weather_warnings",
            WarningsState.from_dict(warnings_section(warning("a", begins=NOW, ends=NOW + timedelta(hours=3)))),
        )
        store.flush()
        reborn = StateStore(self.config).get_state()
        self.assertEqual(reborn.weather_hourly_geosphere.snapshot.status, "ok")
        self.assertEqual(reborn.weather_hourly_geosphere.model_run, "2026-10-26T03:00:00+00:00")
        self.assertEqual(len(reborn.weather_hourly_geosphere.points), 2)
        self.assertEqual(reborn.weather_warnings.items[0].id, "a")

    def test_cache_from_before_the_detail_screen_still_loads(self) -> None:
        legacy = DashboardState.from_dict({}).to_dict()
        for key in DETAIL_SECTIONS:
            legacy.pop(key)
        legacy["weather"]["forecast"] = [
            {"day_label": "Heute", "condition": "Klar", "condition_code": 0, "temperature_max_c": 12, "temperature_min_c": 3}
        ]
        self.config.last_good_path.write_text(json.dumps(legacy), encoding="utf-8")
        state = StateStore(self.config).get_state()
        self.assertEqual(state.weather.forecast[0].day_label, "Heute")
        self.assertIsNone(state.weather.forecast[0].precipitation_probability_max)
        for key in DETAIL_SECTIONS:
            self.assertEqual(getattr(state, key).snapshot.status, "empty")

    def test_failure_in_one_section_leaves_the_others(self) -> None:
        store = StateStore(self.config, monotonic=lambda: 10_000.0)
        store.update_section("weather", replace(DashboardState.from_dict({}).weather, snapshot=ProviderSnapshot(status="ok", updated_at=iso(NOW))))
        store.update_section("weather_warnings", WarningsState.from_dict(warnings_section()))
        store.mark_error("weather_warnings", error_message="500", stale_after_seconds=600, source="geosphere-warnings")
        state = store.get_state()
        self.assertEqual(state.weather.snapshot.status, "ok")
        self.assertEqual(state.weather_warnings.snapshot.status, "stale")


class StatePayloadTest(unittest.TestCase):
    def test_poll_sends_one_hourly_series_and_prepared_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = make_blueprint_app(Path(tmp))
            services = app.extensions["smart_display"]
            services["utcnow"] = lambda: NOW
            store = services["state_store"]
            store.update_section("weather_hourly_openmeteo", HourlyForecastState.from_dict(series("ok", "open-meteo", [1, 2])))
            store.update_section(
                "weather_warnings",
                WarningsState.from_dict(warnings_section(warning("a", begins=NOW, ends=NOW + timedelta(hours=1)))),
            )
            payload = app.test_client().get("/api/state").get_json()
            self.assertNotIn("weather_hourly_geosphere", payload)
            self.assertNotIn("weather_hourly_openmeteo", payload)
            self.assertEqual(payload["weather_hourly"]["snapshot"]["source"], "open-meteo")
            # GeoSphere has not answered yet: Open-Meteo, but no outage claimed.
            self.assertFalse(payload["weather_hourly"]["fallback"])
            self.assertEqual(payload["weather_warnings"]["items"][0]["visible_until"], iso(NOW + timedelta(hours=1)))
            self.assertLess(len(json.dumps(payload)), 40_000, "the poll stays small")

    def test_etag_is_stable_within_the_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = make_blueprint_app(Path(tmp))
            services = app.extensions["smart_display"]
            client = app.test_client()
            services["utcnow"] = lambda: NOW
            first = client.get("/api/state").headers["ETag"]
            services["utcnow"] = lambda: NOW + timedelta(minutes=30)
            self.assertEqual(client.get("/api/state").headers["ETag"], first)


class DetailSchedulingTest(unittest.TestCase):
    def _jobs(self, config):
        noop = lambda: None  # noqa: E731
        return _build_scheduled_jobs(
            config,
            weather_refresh=noop,
            calendar_refresh=noop,
            spotify_refresh=noop,
            screensaver_refresh=noop,
            mock_refresh=noop,
            retry_wait=lambda name: None,
            detail_refresh=dict.fromkeys(DETAIL_SECTIONS, noop),
        )

    def test_intervals_come_from_config_and_none_pause_with_the_screensaver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_app_config(Path(tmp), weather_enabled=True)
            jobs = {job.name: job for job in self._jobs(config)}
            self.assertEqual(jobs["weather_warnings"].interval_seconds, 300)
            self.assertEqual(jobs["weather_nowcast"].interval_seconds, 900)
            self.assertEqual(jobs["weather_hourly_geosphere"].interval_seconds, 3600)
            for name in ("weather_warnings", "weather_nowcast", "weather_hourly_geosphere"):
                self.assertIsNone(jobs[name].pause_group)
                self.assertIsNotNone(jobs[name].retry_after)

    def test_switches_remove_their_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = make_app_config(Path(tmp), weather_enabled=True)
            config.weather = replace(config.weather, geosphere_enabled=False, warnings_enabled=False)
            self.assertEqual([job.name for job in self._jobs(config)], ["weather"])

    def test_requests_stay_far_below_the_geosphere_limit(self) -> None:
        # 240 requests per hour (dataset API rate-limit headers, 2026-10-26).
        with tempfile.TemporaryDirectory() as tmp:
            intervals = make_app_config(Path(tmp)).refresh_intervals
            per_hour = 3600 / intervals.weather_hourly_seconds + 3600 / intervals.weather_nowcast_seconds
            self.assertLessEqual(per_hour, 10)


class ConditionCodesTest(unittest.TestCase):
    def test_home_and_week_name_a_code_the_same(self) -> None:
        from smart_display.providers.weather_openmeteo import WEATHER_CODES

        for code, label in WEATHER_CODES.items():
            self.assertEqual(wmo_condition(code)[1], label, code)
        self.assertEqual(WEATHER_CODES[71], "Leichter Schnee")

    def test_the_two_code_families_are_not_interchangeable(self) -> None:
        self.assertEqual(geosphere_condition(3)[0], "partly-cloudy")
        self.assertEqual(wmo_condition(3)[0], "cloudy")
        self.assertEqual(geosphere_condition(7), ("cloudy", "Hochnebel"))
        self.assertEqual(geosphere_condition(11)[0], "sleet")
        self.assertEqual(wmo_condition(56)[0], "sleet")

    def test_missing_or_odd_codes_are_unknown_never_sun(self) -> None:
        for code in (None, 0, 33, 3.5, "x", float("nan"), True):
            with self.subTest(code=code):
                self.assertEqual(geosphere_condition(code)[0], "unknown")
        for code in (None, 4, 100):
            self.assertEqual(wmo_condition(code)[0], "unknown")

    def test_night_variant_only_for_clear_and_partly_cloudy(self) -> None:
        self.assertEqual(geosphere_condition(1, is_day=False)[0], "clear-night")
        self.assertEqual(geosphere_condition(3.0, is_day=False)[0], "partly-cloudy-night")
        self.assertEqual(geosphere_condition(9, is_day=False)[0], "rain")
        self.assertEqual(wmo_condition(0, is_day=None)[0], "clear")


class SunTest(unittest.TestCase):
    def test_vienna_matches_open_meteo_within_three_minutes(self) -> None:
        recorded = json.loads((FIXTURES / "openmeteo_vienna.json").read_text())
        for index in range(7):
            day = date(2026, 10, 26) + timedelta(days=index)
            rise, set_ = sun_times(day, 48.2082, 16.3738)
            om_rise = datetime.fromtimestamp(recorded["daily"]["sunrise"][index], timezone.utc)
            om_set = datetime.fromtimestamp(recorded["daily"]["sunset"][index], timezone.utc)
            self.assertLess(abs((rise - om_rise).total_seconds()), 180)
            self.assertLess(abs((set_ - om_set).total_seconds()), 180)

    def test_polar_day_and_night(self) -> None:
        self.assertEqual(sun_times(date(2026, 12, 21), 78.2, 15.6), (None, None))
        self.assertFalse(is_daylight(datetime(2026, 12, 21, 12, tzinfo=timezone.utc), 78.2, 15.6))
        self.assertTrue(is_daylight(datetime(2026, 6, 21, 0, tzinfo=timezone.utc), 78.2, 15.6))


if __name__ == "__main__":
    unittest.main()
