"""#22: the four weather-detail sources against recorded real responses.

Fixtures in tests/fixtures/weather are untouched API answers (see the README
there); variants — nulls, partial answers, invalid payloads — are made from
them here. No test reaches the network: every provider gets a FakeHttpClient.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from smart_display.http_client import HttpError
from smart_display.providers._fetch import Backoff, FetchError, parse_retry_after
from smart_display.providers.weather_geosphere import (
    ClaefProvider,
    IncaProvider,
    WarningsProvider,
    parse_warning,
    wind_from_components,
)
from smart_display.providers.weather_openmeteo import OpenMeteoProvider
from smart_display.state_store import FIRST_FETCH_GRACE_SECONDS, StateStore
from tests._support import FakeClock, FakeHttpClient, make_app_config

FIXTURES = Path(__file__).parent / "fixtures" / "weather"
VIENNA = ZoneInfo("Europe/Vienna")
# The fixtures were recorded at 09:22 UTC on 2026-10-26.
RECORDED = datetime(2026, 10, 26, 9, 22, tzinfo=timezone.utc)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock(wall=RECORDED, monotonic=1000.0)
        self.config = make_app_config(self.tmp, weather_enabled=True)
        self.store = StateStore(self.config, monotonic=self.clock.monotonic)
        self.http = FakeHttpClient()

    def past_grace(self) -> None:
        self.clock.advance(FIRST_FETCH_GRACE_SECONDS + 1)

    def provider(self, cls):
        return cls(self.config, self.store, self.http, now=self.clock.now, monotonic=self.clock.monotonic)

    def section(self, name: str):
        return getattr(self.store.get_state(), name)


# ---- Open-Meteo -----------------------------------------------------------------


class OpenMeteoDetailTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.om = self.provider(OpenMeteoProvider)
        self.url = self.om.request_url()

    def test_request_asks_for_seven_days_hourly_and_unix_times(self) -> None:
        self.assertIn("forecast_days=7", self.url)
        self.assertIn("timeformat=unixtime", self.url)
        self.assertIn("wind_speed_unit=kmh", self.url)
        self.assertIn("latitude=48.2082", self.url)
        for name in ("precipitation_probability_max", "precipitation_sum", "sunrise", "wind_gusts_10m"):
            self.assertIn(name, self.url)

    def test_real_response_fills_week_now_and_hourly_fallback(self) -> None:
        self.http.add_response("GET", self.url, body=load("openmeteo_vienna.json"))
        self.om.refresh()
        weather = self.section("weather")
        self.assertEqual(weather.snapshot.status, "ok")
        self.assertEqual([d.date for d in weather.forecast][:3], ["2026-10-26", "2026-10-27", "2026-10-28"])
        self.assertEqual(len(weather.forecast), 7)
        self.assertEqual(weather.forecast[0].day_label, "Heute")
        self.assertEqual(weather.forecast[2].precipitation_probability_max, 45)
        self.assertTrue(weather.forecast[0].sunrise.startswith("2026-10-26T05:"))
        self.assertEqual(weather.observed_at, "2026-10-26T09:15:00+00:00")
        self.assertAlmostEqual(weather.wind_speed_kmh, 15.5)
        hourly = self.section("weather_hourly_openmeteo")
        self.assertEqual(hourly.snapshot.status, "ok")
        # From the current hour to the end of tomorrow, local: 23:00 UTC.
        self.assertEqual(hourly.points[0].time, "2026-10-26T09:00:00+00:00")
        self.assertEqual(hourly.points[-1].time, "2026-10-27T23:00:00+00:00")
        self.assertIsNone(hourly.model_run)
        self.assertEqual(hourly.points[0].precipitation_probability, 0)

    def test_daily_icon_is_the_day_variant_even_at_night(self) -> None:
        payload = load("openmeteo_vienna.json")
        payload["current"]["is_day"] = 0
        payload["current"]["weather_code"] = 0
        payload["daily"]["weather_code"][0] = 0
        self.http.add_response("GET", self.url, body=payload)
        self.om.refresh()
        weather = self.section("weather")
        self.assertEqual(weather.icon, "clear-night")
        self.assertEqual(weather.forecast[0].icon, "clear")

    def test_nulls_stay_unknown_never_zero(self) -> None:
        payload = load("openmeteo_vienna.json")
        payload["daily"]["precipitation_sum"][1] = None
        payload["daily"]["precipitation_probability_max"][1] = None
        payload["daily"]["weather_code"][1] = None
        payload["current"]["wind_speed_10m"] = None
        payload["hourly"]["precipitation"] = [None] * len(payload["hourly"]["precipitation"])
        self.http.add_response("GET", self.url, body=payload)
        self.om.refresh()
        weather = self.section("weather")
        self.assertIsNone(weather.forecast[1].precipitation_sum_mm)
        self.assertIsNone(weather.forecast[1].precipitation_probability_max)
        self.assertEqual(weather.forecast[1].icon, "unknown")
        self.assertIsNone(weather.wind_speed_kmh)
        hourly = self.section("weather_hourly_openmeteo")
        self.assertTrue(hourly.points)
        self.assertTrue(all(p.precipitation_mm is None for p in hourly.points))

    def test_dst_day_has_twentyfive_hours(self) -> None:
        # 25.10.2026, the autumn change, seen from the morning before.
        self.clock.set_wall(datetime(2026, 10, 24, 5, 30, tzinfo=timezone.utc))
        self.http.add_response("GET", self.url, body=load("openmeteo_dst_window.json"))
        self.om.refresh()
        hourly = self.section("weather_hourly_openmeteo")
        on_25th = [
            p for p in hourly.points
            if (datetime.fromisoformat(p.time) - timedelta(minutes=1)).astimezone(VIENNA).date().isoformat()
            == "2026-10-25"
        ]
        self.assertEqual(len(on_25th), 25)
        # Daily dates come out right despite Open-Meteo's fixed-offset days.
        self.assertEqual(self.section("weather").forecast[1].date, "2026-10-25")

    def test_error_body_is_a_permanent_error_not_a_success(self) -> None:
        self.past_grace()
        self.http.add_response(
            "GET", self.url, status=400,
            body={"error": True, "reason": "Cannot initialize WeatherVariable from invalid String value x"},
        )
        self.om.refresh()
        self.assertEqual(self.section("weather").snapshot.status, "error")
        self.assertEqual(self.section("weather_hourly_openmeteo").snapshot.status, "error")
        self.assertIsNone(self.section("weather").snapshot.updated_at)

    def test_empty_answer_is_not_stored_as_success(self) -> None:
        self.http.queue("GET", self.url, body=load("openmeteo_vienna.json"))
        self.http.queue("GET", self.url, body={})
        self.om.refresh()
        good_at = self.section("weather").snapshot.updated_at
        self.past_grace()
        self.om.refresh()
        weather = self.section("weather")
        self.assertEqual(weather.snapshot.status, "stale")
        self.assertEqual(weather.snapshot.updated_at, good_at)
        self.assertEqual(len(weather.forecast), 7, "last good data must survive")

    def test_timeout_keeps_cadence_and_recovers(self) -> None:
        self.http.queue("GET", self.url, body=load("openmeteo_vienna.json"))
        self.om.refresh()
        self.past_grace()
        route = self.http._route("GET", self.url)
        route.error = HttpError("timed out")
        self.om.refresh()
        self.assertEqual(self.section("weather").snapshot.status, "stale")
        route.error = None
        self.http.queue("GET", self.url, body=load("openmeteo_vienna.json"))
        self.om.refresh()  # no backoff after a network error
        self.assertEqual(self.section("weather").snapshot.status, "ok")
        self.assertEqual(len(self.http.calls), 3)

    def test_429_honours_retry_after_then_recovers(self) -> None:
        self.http.queue("GET", self.url, body=load("openmeteo_vienna.json"))
        self.http.queue("GET", self.url, status=429, body=b"", headers={"Retry-After": "3600"})
        self.http.queue("GET", self.url, body=load("openmeteo_vienna.json"))
        self.om.refresh()
        self.past_grace()
        self.om.refresh()
        self.assertEqual(self.section("weather").snapshot.status, "stale")
        self.assertIn("429", self.section("weather").snapshot.error_message)
        calls = len(self.http.calls)
        # The next two scheduled ticks (15 min each) are inside the hour.
        for _ in range(2):
            self.clock.advance(900)
            self.om.refresh()
        self.assertEqual(len(self.http.calls), calls, "must not call while told to wait")
        self.assertEqual(self.section("weather").snapshot.status, "stale")
        self.clock.advance(3600)
        self.om.refresh()
        self.assertEqual(len(self.http.calls), calls + 1)
        self.assertEqual(self.section("weather").snapshot.status, "ok")

    def test_failure_leaves_warnings_and_geosphere_alone(self) -> None:
        self.past_grace()
        self.http.add_response("GET", self.url, status=503, body=b"")
        self.om.refresh()
        state = self.store.get_state()
        self.assertEqual(state.weather.snapshot.status, "error")
        self.assertEqual(state.weather_warnings.snapshot.status, "empty")
        self.assertEqual(state.weather_hourly_geosphere.snapshot.status, "empty")


# ---- C-LAEF ---------------------------------------------------------------------


class ClaefTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.claef = self.provider(ClaefProvider)
        self.url = self.claef.request_url()

    def test_request_is_one_point_with_the_documented_parameters(self) -> None:
        self.assertIn("/timeseries/forecast/nwp-v2-1h-1km?", self.url)
        self.assertIn("lat_lon=48.2082%2C16.3738", self.url)
        self.assertIn("parameters=2t%2C10u%2C10v%2C10fg%2Ctp%2Csy", self.url)
        self.assertIn("output_format=geojson", self.url)

    def test_real_response_becomes_hourly_series(self) -> None:
        self.http.add_response("GET", self.url, body=load("claef_vienna.json"))
        self.claef.refresh()
        series = self.section("weather_hourly_geosphere")
        self.assertEqual(series.snapshot.status, "ok")
        self.assertEqual(series.model_run, "2026-10-26T03:00:00+00:00")
        self.assertEqual(series.points[0].time, "2026-10-26T09:00:00+00:00")
        # The model runs to 28.10. 15:00 UTC; only today and tomorrow are kept.
        self.assertEqual(series.points[-1].time, "2026-10-27T23:00:00+00:00")
        self.assertEqual(len(series.points), 39)
        first = series.points[0]
        self.assertEqual(first.temperature_c, 11.0)
        self.assertIsNone(first.precipitation_probability, "C-LAEF has none — never invent one")
        self.assertAlmostEqual(first.wind_speed_kmh, 16.68, places=1)
        self.assertAlmostEqual(first.wind_direction_deg, 283.7, places=1)
        self.assertAlmostEqual(first.wind_gusts_kmh, 35.64, places=2)
        self.assertEqual(first.icon, "clear")  # sy 1 at 10:00 local
        by_time = {p.time: p for p in series.points}
        # sy 1 after sunset (16:47 local) gets the moon; sy 3 the night cloud;
        # sy 5 stays a cloud — only clear and partly cloudy have night forms.
        self.assertEqual(by_time["2026-10-26T20:00:00+00:00"].icon, "clear-night")
        self.assertEqual(by_time["2026-10-26T23:00:00+00:00"].icon, "partly-cloudy-night")
        self.assertEqual(by_time["2026-10-26T17:00:00+00:00"].icon, "cloudy")
        self.assertEqual(by_time["2026-10-26T13:00:00+00:00"].icon, "partly-cloudy")
        self.assertEqual(by_time["2026-10-26T13:00:00+00:00"].condition, "Teilweise bewölkt")

    def test_unknown_or_missing_symbol_is_unknown_never_sun(self) -> None:
        payload = load("claef_vienna.json")
        symbols = payload["features"][0]["properties"]["parameters"]["sy"]["data"]
        symbols[0] = None
        symbols[1] = 99.0
        symbols[2] = 3.5
        self.http.add_response("GET", self.url, body=payload)
        self.claef.refresh()
        points = self.section("weather_hourly_geosphere").points
        self.assertEqual([p.icon for p in points[:3]], ["unknown", "unknown", "unknown"])

    def test_changed_unit_counts_as_missing(self) -> None:
        payload = load("claef_vienna.json")
        payload["features"][0]["properties"]["parameters"]["2t"]["unit"] = "K"
        self.past_grace()
        self.http.add_response("GET", self.url, body=payload)
        self.claef.refresh()
        self.assertEqual(self.section("weather_hourly_geosphere").snapshot.status, "error")

    def test_partial_answer_keeps_gaps_and_short_horizon(self) -> None:
        payload = load("claef_vienna.json")
        payload["timestamps"] = payload["timestamps"][:6]
        params = payload["features"][0]["properties"]["parameters"]
        for entry in params.values():
            entry["data"] = entry["data"][:6]
        params["tp"]["data"][2] = None
        params["2t"]["data"][3] = None
        del params["10fg"]
        self.http.add_response("GET", self.url, body=payload)
        self.claef.refresh()
        series = self.section("weather_hourly_geosphere")
        self.assertEqual(series.valid_to, "2026-10-26T14:00:00+00:00")
        self.assertIsNone(series.points[2].precipitation_mm)
        self.assertIsNone(series.points[3].temperature_c)
        self.assertTrue(all(p.wind_gusts_kmh is None for p in series.points))

    def test_all_null_answer_is_an_error_and_keeps_last_good(self) -> None:
        good = load("claef_vienna.json")
        bad = copy.deepcopy(good)
        bad["features"][0]["properties"]["parameters"]["2t"]["data"] = [None] * len(bad["timestamps"])
        self.http.queue("GET", self.url, body=good)
        self.http.queue("GET", self.url, body=bad)
        self.claef.refresh()
        self.past_grace()
        self.claef.refresh()
        series = self.section("weather_hourly_geosphere")
        self.assertEqual(series.snapshot.status, "stale")
        self.assertEqual(len(series.points), 39)

    def test_invalid_payload_shapes_are_errors(self) -> None:
        self.past_grace()
        for body in ([], {"timestamps": []}, {"timestamps": ["x"], "features": []}, "garbage"):
            with self.subTest(body=body):
                self.http.add_response("GET", self.url, body=body)
                self.claef.refresh()
                self.assertEqual(self.section("weather_hourly_geosphere").snapshot.status, "error")

    def test_point_outside_dataset_is_disabled_not_an_outage(self) -> None:
        self.http.add_response("GET", self.url, status=400, body=load("claef_outside.json"))
        self.claef.refresh()
        snapshot = self.section("weather_hourly_geosphere").snapshot
        self.assertEqual(snapshot.status, "disabled")

    def test_bad_parameter_is_a_permanent_error(self) -> None:
        self.http.add_response("GET", self.url, status=400, body=load("claef_bad_param.json"))
        self.claef.refresh()
        self.assertEqual(self.section("weather_hourly_geosphere").snapshot.status, "error")

    def test_server_errors_back_off_exponentially(self) -> None:
        # Hourly ticks: calls after 1, 2 and 4 intervals, never closer.
        self.http.add_response("GET", self.url, status=503, body=b"")
        called_at = []
        for tick in range(8):
            before = len(self.http.calls)
            self.claef.refresh()
            if len(self.http.calls) > before:
                called_at.append(tick)
            self.clock.advance(3600)
        self.assertEqual(called_at, [0, 1, 3, 7])

    def test_dst_tomorrow_has_twentyfive_hours(self) -> None:
        self.clock.set_wall(datetime(2026, 10, 24, 6, 10, tzinfo=timezone.utc))
        start = datetime(2026, 10, 24, 6, tzinfo=timezone.utc)
        stamps = [(start + timedelta(hours=h)).isoformat(timespec="minutes") for h in range(61)]
        payload = {
            "reference_time": "2026-10-24T03:00+00:00",
            "timestamps": stamps,
            "features": [{"properties": {"parameters": {
                "2t": {"unit": "degree Celsius", "data": [10.0] * 61},
                "tp": {"unit": "kg m-2", "data": [0.0] * 61},
                "sy": {"unit": "1", "data": [5.0] * 61},
            }}}],
        }
        self.http.add_response("GET", self.url, body=payload)
        self.claef.refresh()
        days = {}
        for point in self.section("weather_hourly_geosphere").points:
            day = (datetime.fromisoformat(point.time) - timedelta(minutes=1)).astimezone(VIENNA).date()
            days[day.isoformat()] = days.get(day.isoformat(), 0) + 1
        self.assertEqual(days["2026-10-25"], 25)
        self.assertNotIn("2026-10-26", days)

    def test_disabled_switch_reports_disabled(self) -> None:
        self.config.weather = replace(self.config.weather, geosphere_enabled=False)
        self.claef.refresh()
        self.assertEqual(self.section("weather_hourly_geosphere").snapshot.status, "disabled")
        self.assertEqual(self.http.calls, [])


class WindTest(unittest.TestCase):
    def test_components_to_meteorological_direction(self) -> None:
        cases = {(0.0, -5.0): 0.0, (-5.0, 0.0): 90.0, (0.0, 5.0): 180.0, (5.0, 0.0): 270.0}
        for (u, v), expected in cases.items():
            speed, direction = wind_from_components(u, v)
            self.assertAlmostEqual(direction, expected, places=6)
            self.assertAlmostEqual(speed, 18.0)

    def test_calm_has_no_direction_and_missing_is_unknown(self) -> None:
        self.assertEqual(wind_from_components(0.0, 0.0), (0.0, None))
        self.assertEqual(wind_from_components(None, 1.0), (None, None))


# ---- INCA -----------------------------------------------------------------------


class IncaTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.inca = self.provider(IncaProvider)
        self.url = self.inca.request_url()

    def test_rainy_answer_is_kept_as_fifteen_minute_intervals(self) -> None:
        self.assertIn("nowcast-v1-15min-1km", self.url)
        self.assertIn("parameters=rr", self.url)
        self.http.add_response("GET", self.url, body=load("inca_linz_rain.json"))
        self.inca.refresh()
        nowcast = self.section("weather_nowcast")
        self.assertEqual(nowcast.snapshot.status, "ok")
        self.assertEqual(nowcast.model_run, "2026-10-26T09:00:00+00:00")
        self.assertEqual(nowcast.interval_minutes, 15)
        self.assertEqual(len(nowcast.points), 12)
        self.assertEqual(nowcast.points[0].ends_at, "2026-10-26T09:15:00+00:00")
        self.assertEqual(nowcast.points[0].precipitation_mm, 0.38)
        self.assertEqual(nowcast.points[-1].ends_at, "2026-10-26T12:00:00+00:00")

    def test_dry_answer_is_zero_not_missing(self) -> None:
        self.http.add_response("GET", self.url, body=load("inca_vienna_dry.json"))
        self.inca.refresh()
        self.assertTrue(all(p.precipitation_mm == 0.0 for p in self.section("weather_nowcast").points))

    def test_negative_and_null_values_stay_unknown(self) -> None:
        payload = load("inca_linz_rain.json")
        payload["features"][0]["properties"]["parameters"]["rr"]["data"][0] = -1.0
        payload["features"][0]["properties"]["parameters"]["rr"]["data"][1] = None
        self.http.add_response("GET", self.url, body=payload)
        self.inca.refresh()
        points = self.section("weather_nowcast").points
        self.assertIsNone(points[0].precipitation_mm)
        self.assertIsNone(points[1].precipitation_mm)

    def test_all_null_is_an_error(self) -> None:
        payload = load("inca_linz_rain.json")
        payload["features"][0]["properties"]["parameters"]["rr"]["data"] = [None] * 12
        self.past_grace()
        self.http.add_response("GET", self.url, body=payload)
        self.inca.refresh()
        self.assertEqual(self.section("weather_nowcast").snapshot.status, "error")
        self.assertEqual(self.section("weather_nowcast").points, [])


# ---- Warnings -------------------------------------------------------------------


def warning_feature(warnid=1, chgid=1, wtype=1, level=2, start=None, end=None, begin_text=None, end_text=None):
    raw = {}
    if start is not None:
        raw["start"] = str(int(start.timestamp()))
    if end is not None:
        raw["end"] = str(int(end.timestamp()))
    return {
        "type": "Warning",
        "properties": {
            "warnid": warnid,
            "chgid": chgid,
            "warntypid": wtype,
            "warnstufeid": level,
            "begin": begin_text,
            "end": end_text,
            "text": "Text",
            "auswirkungen": "Auswirkungen",
            "empfehlungen": None,
            "meteotext": "Wetterlage",
            "rawinfo": raw,
        },
    }


def warnings_payload(*features):
    payload = load("warnings_vienna_none.json")
    payload["properties"]["warnings"] = list(features)
    return payload


class WarningsTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.warn = self.provider(WarningsProvider)
        self.url = self.warn.request_url()

    def test_request_is_the_documented_point_query(self) -> None:
        self.assertEqual(
            self.url,
            "https://warnungen.zamg.at/wsapp/api/getWarningsForCoords?lon=16.3738&lat=48.2082&lang=de",
        )

    def test_confirmed_empty_list_is_an_all_clear(self) -> None:
        self.http.add_response("GET", self.url, body=load("warnings_vienna_none.json"))
        self.warn.refresh()
        warnings = self.section("weather_warnings")
        self.assertEqual(warnings.snapshot.status, "ok")
        self.assertEqual(warnings.items, [])
        self.assertEqual(warnings.municipality_id, "90101")
        self.assertEqual(warnings.municipality_name, "Wien-Innere Stadt")

    def test_documented_example_parses_with_exact_validity(self) -> None:
        self.clock.set_wall(datetime(2023, 3, 27, 7, 0, tzinfo=timezone.utc))
        self.http.add_response("GET", self.url, body=load("warnings_documented_example.json"))
        self.warn.refresh()
        warnings = self.section("weather_warnings")
        self.assertEqual(warnings.municipality_name, "Schwechat")
        self.assertEqual([w.id for w in warnings.items], ["4149", "4150"])
        first = warnings.items[0]
        # rawinfo 1679896800 == "27.03.2023 08:00" Vienna summer time.
        self.assertEqual(first.begins_at, "2023-03-27T06:00:00+00:00")
        self.assertEqual(first.ends_at, "2023-03-27T16:00:00+00:00")
        self.assertEqual((first.type, first.type_label, first.level, first.level_label), (1, "Wind", 1, "Gelb"))
        self.assertTrue(first.recommendations.startswith("* Seien Sie"))
        self.assertEqual(first.version, 6)

    def test_ended_warnings_are_dropped_end_is_exclusive(self) -> None:
        self.clock.set_wall(datetime(2023, 3, 27, 16, 0, tzinfo=timezone.utc))
        self.http.add_response("GET", self.url, body=load("warnings_documented_example.json"))
        self.warn.refresh()
        self.assertEqual([w.id for w in self.section("weather_warnings").items], ["4150"])

    def test_local_strings_are_the_fallback_for_validity(self) -> None:
        feature = warning_feature(begin_text="27.10.2026 08:00", end_text="27.10.2026 18:00")
        warning = parse_warning(feature)
        self.assertEqual(warning.begins_at, "2026-10-27T07:00:00+00:00")  # CET again
        self.assertEqual(warning.ends_at, "2026-10-27T17:00:00+00:00")

    def test_inverted_or_missing_end_has_no_end(self) -> None:
        now = RECORDED
        inverted = parse_warning(warning_feature(start=now, end=now - timedelta(hours=1)))
        self.assertIsNone(inverted.ends_at)
        open_ended = parse_warning(warning_feature(start=now))
        self.assertIsNone(open_ended.ends_at)

    def test_newer_revision_of_the_same_warning_wins(self) -> None:
        start = RECORDED + timedelta(hours=1)
        payload = warnings_payload(
            warning_feature(warnid=7, chgid=1, level=1, start=start, end=start + timedelta(hours=5)),
            warning_feature(warnid=7, chgid=3, level=2, start=start, end=start + timedelta(hours=5)),
        )
        self.http.add_response("GET", self.url, body=payload)
        self.warn.refresh()
        items = self.section("weather_warnings").items
        self.assertEqual([(w.id, w.version, w.level) for w in items], [("7", 3, 2)])

    def test_revision_that_ended_the_warning_wins_over_the_older_one(self) -> None:
        start = RECORDED - timedelta(hours=3)
        payload = warnings_payload(
            warning_feature(warnid=7, chgid=1, start=start, end=RECORDED + timedelta(hours=5)),
            warning_feature(warnid=7, chgid=2, start=start, end=RECORDED - timedelta(hours=1)),
        )
        self.http.add_response("GET", self.url, body=payload)
        self.warn.refresh()
        self.assertEqual(self.section("weather_warnings").items, [])

    def test_missing_list_is_an_error_not_an_all_clear(self) -> None:
        start = RECORDED - timedelta(hours=1)
        self.http.queue("GET", self.url, body=warnings_payload(warning_feature(start=start, end=start + timedelta(hours=6))))
        broken = load("warnings_vienna_none.json")
        del broken["properties"]["warnings"]
        self.http.queue("GET", self.url, body=broken)
        self.warn.refresh()
        self.past_grace()
        self.warn.refresh()
        warnings = self.section("weather_warnings")
        self.assertEqual(warnings.snapshot.status, "stale")
        self.assertEqual(len(warnings.items), 1, "the last confirmed warning stays")

    def test_successful_empty_answer_clears_previous_warnings(self) -> None:
        start = RECORDED - timedelta(hours=1)
        self.http.queue("GET", self.url, body=warnings_payload(warning_feature(start=start, end=start + timedelta(hours=6))))
        self.http.queue("GET", self.url, body=load("warnings_vienna_none.json"))
        self.warn.refresh()
        self.assertEqual(len(self.section("weather_warnings").items), 1)
        self.warn.refresh()
        self.assertEqual(self.section("weather_warnings").items, [])
        self.assertEqual(self.section("weather_warnings").snapshot.status, "ok")

    def test_unreadable_entry_rejects_the_whole_answer(self) -> None:
        self.past_grace()
        self.http.add_response("GET", self.url, body=warnings_payload({"type": "Warning"}))
        self.warn.refresh()
        self.assertEqual(self.section("weather_warnings").snapshot.status, "error")

    def test_no_municipality_is_disabled(self) -> None:
        self.http.add_response("GET", self.url, status=404, body=load("warnings_outside.json"))
        self.warn.refresh()
        self.assertEqual(self.section("weather_warnings").snapshot.status, "disabled")

    def test_other_404_is_an_outage_not_a_missing_municipality(self) -> None:
        self.past_grace()
        self.http.add_response("GET", self.url, status=404, body=b"<html>Not Found</html>")
        self.warn.refresh()
        self.assertEqual(self.section("weather_warnings").snapshot.status, "error")

    def test_warning_failure_does_not_touch_weather(self) -> None:
        self.past_grace()
        self.http.add_response("GET", self.url, status=500, body=b"")
        self.warn.refresh()
        state = self.store.get_state()
        self.assertEqual(state.weather_warnings.snapshot.status, "error")
        self.assertEqual(state.weather.snapshot.status, "empty")


# ---- Fetch helper -----------------------------------------------------------------


class FetchHelperTest(unittest.TestCase):
    def test_retry_after_forms_and_bounds(self) -> None:
        now = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after("Mon, 26 Oct 2026 09:10:00 GMT", now=now), 600.0)
        self.assertEqual(parse_retry_after("-5"), 0.0)
        self.assertEqual(parse_retry_after("999999"), 6 * 3600.0)
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))

    def test_backoff_ignores_network_errors_and_resets_on_success(self) -> None:
        clock = FakeClock(monotonic=0.0)
        backoff = Backoff(900, monotonic=clock.monotonic)
        backoff.failure(FetchError("down"))
        self.assertTrue(backoff.ready())
        backoff.failure(FetchError("x", status=429, retry_after=1800))
        self.assertFalse(backoff.ready())
        clock.advance(1800)
        self.assertTrue(backoff.ready())
        backoff.failure(FetchError("x", status=503))
        backoff.success()
        self.assertTrue(backoff.ready())


if __name__ == "__main__":
    unittest.main()
