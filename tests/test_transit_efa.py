"""Departures from the Linz AG EFA (#27).

The fixtures in tests/fixtures/transit are real ``XML_DM_REQUEST`` answers
(2026-11-02, for Tuesday 2026-11-03 from 07:00), cut to the first entries.
At Schiffswerft the 27 and 72 leave from the same stop and must be filtered
out; Hafenportal only has the 46.
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

from flask import Flask

from smart_display.config import TransitConfig, TransitStop, load_config
from smart_display.http_client import HttpError
from smart_display.providers.transit_efa import (
    CACHE_TTL_SECONDS,
    DM_LIMIT,
    MAX_BYTES,
    SPARE_DEPARTURES,
    STALE_MAX_AGE_SECONDS,
    TransitProvider,
    parse_departures,
    select_upcoming,
)
from smart_display.web.routes import create_blueprint
from tests._support import FakeClock, FakeHttpClient, make_app_config, make_blueprint_app, make_state_store

VIENNA = ZoneInfo("Europe/Vienna")
FIXTURES = Path(__file__).parent / "fixtures" / "transit"
SCHIFFSWERFT = json.loads((FIXTURES / "efa_dm_schiffswerft.json").read_text(encoding="utf-8"))
HAFENPORTAL = json.loads((FIXTURES / "efa_dm_hafenportal.json").read_text(encoding="utf-8"))
STOPS = [
    TransitStop(stop_id="60501350", line="70", label="Schiffswerft"),
    TransitStop(stop_id="60500097", line="46", label="Hafenportal"),
]
BASE = "https://www.linzag.at/static/XML_DM_REQUEST?outputFormat=JSON&language=de&type_dm=stop"
URL_70 = f"{BASE}&name_dm=60501350&mode=direct&useRealtime=1&limit={DM_LIMIT}"
URL_46 = f"{BASE}&name_dm=60500097&mode=direct&useRealtime=1&limit={DM_LIMIT}"


def vienna(*args: int) -> datetime:
    return datetime(*args, tzinfo=VIENNA)


def times(departures: list[dict]) -> list[str]:
    return [d["planned"][11:16] for d in departures]


class ParseDeparturesTest(unittest.TestCase):
    def test_only_the_configured_line(self) -> None:
        departures = parse_departures(SCHIFFSWERFT, "70", VIENNA)
        self.assertEqual(times(departures), ["07:06", "07:26", "07:49"])
        self.assertEqual(departures[0], {
            "planned": "2026-11-03T07:06:00+01:00",
            "expected": None,
            "direction": "Stadtfriedhof",
        })
        self.assertEqual(departures[2]["direction"], "Europaplatz")

    def test_a_stop_with_one_line(self) -> None:
        departures = parse_departures(HAFENPORTAL, "46", VIENNA)
        self.assertEqual(times(departures), ["07:12", "07:27", "07:42", "07:57", "08:12", "08:27"])
        self.assertEqual({d["direction"] for d in departures}, {"Hauptbahnhof"})

    def test_line_absent_is_empty_not_an_error(self) -> None:
        self.assertEqual(parse_departures(HAFENPORTAL, "70", VIENNA), [])

    def test_real_time_wins_and_sorts(self) -> None:
        # Synthetic: Linz AG's real-time format was not observable at 00:40.
        payload = copy.deepcopy(SCHIFFSWERFT)
        first = next(d for d in payload["departureList"] if d["servingLine"]["number"] == "70")
        first["realDateTime"] = {**first["dateTime"], "minute": "30"}
        departures = parse_departures(payload, "70", VIENNA)
        self.assertEqual(times(departures), ["07:26", "07:06", "07:49"])
        self.assertEqual(departures[1]["expected"], "2026-11-03T07:30:00+01:00")

    def test_summer_time_offset(self) -> None:
        payload = copy.deepcopy(HAFENPORTAL)
        payload["departureList"][0]["dateTime"].update({"month": "7", "day": "1"})
        self.assertEqual(parse_departures(payload, "46", VIENNA)[0]["planned"], "2026-07-01T07:12:00+02:00")

    def test_efa_list_quirks(self) -> None:
        single = HAFENPORTAL["departureList"][0]
        self.assertEqual(len(parse_departures({"departureList": {"departure": single}}, "46", VIENNA)), 1)
        self.assertEqual(parse_departures({"departureList": None}, "46", VIENNA), [])

    def test_not_a_departure_answer(self) -> None:
        for payload in (None, [], "ok", {"stopFinder": {}}, {"departureList": "x"}):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_departures(payload, "46", VIENNA))

    def test_broken_entries_are_skipped_not_invented(self) -> None:
        good = HAFENPORTAL["departureList"][0]
        payload = {"departureList": [
            "junk",
            {**good, "servingLine": None},
            {**good, "dateTime": {"year": "2026", "month": "11"}},
            {**good, "dateTime": {**good["dateTime"], "hour": "25"}},
            {**good, "realDateTime": "soon"},
        ]}
        departures = parse_departures(payload, "46", VIENNA)
        self.assertEqual(len(departures), 1)
        self.assertIsNone(departures[0]["expected"])


class SelectUpcomingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.departures = parse_departures(SCHIFFSWERFT, "70", VIENNA)

    def test_counts_until_the_end_of_its_minute(self) -> None:
        kept = select_upcoming(self.departures, vienna(2026, 11, 3, 7, 7, 0), 0, 5)
        self.assertEqual(times(kept), ["07:06", "07:26", "07:49"])
        kept = select_upcoming(self.departures, vienna(2026, 11, 3, 7, 7, 1), 0, 5)
        self.assertEqual(times(kept), ["07:26", "07:49"])

    def test_walk_time_drops_what_cannot_be_reached(self) -> None:
        kept = select_upcoming(self.departures, vienna(2026, 11, 3, 7, 2), 5, 5)
        self.assertEqual(times(kept), ["07:26", "07:49"])
        kept = select_upcoming(self.departures, vienna(2026, 11, 3, 7, 1), 5, 5)
        self.assertEqual(times(kept), ["07:06", "07:26", "07:49"])

    def test_limit(self) -> None:
        self.assertEqual(len(select_upcoming(self.departures, vienna(2026, 11, 3, 7, 0), 0, 2)), 2)


class TransitProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Tuesday 07:00 in Vienna (CET).
        self.clock = FakeClock(wall=vienna(2026, 11, 3, 7, 0).astimezone(timezone.utc))
        self.http = FakeHttpClient()
        base = make_app_config(Path(self._tmp.name))
        self.config = replace(base, transit=TransitConfig(stops=list(STOPS)))

    def _provider(self, config=None) -> TransitProvider:
        return TransitProvider(
            config or self.config,
            http_client=self.http,
            monotonic=self.clock.monotonic,
            utcnow=self.clock.now,
        )

    def _answer_both(self) -> None:
        self.http.add_response("GET", URL_70, body=SCHIFFSWERFT)
        self.http.add_response("GET", URL_46, body=HAFENPORTAL)

    def test_ok(self) -> None:
        self._answer_both()
        result = self._provider().status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["updated_at"], "2026-11-03T06:00:00+00:00")
        first, second = result["data"]["stops"]
        self.assertEqual((first["line"], first["label"], first["status"]), ("70", "Schiffswerft", "ok"))
        self.assertEqual(times(first["departures"]), ["07:06", "07:26", "07:49"])
        self.assertEqual(len(second["departures"]), 3 + SPARE_DEPARTURES)
        self.assertEqual(second["walk_minutes"], 0)
        call = self.http.calls[0]
        self.assertEqual((call.timeout, call.max_bytes), (4.0, MAX_BYTES))

    def test_outside_the_window_nothing_is_fetched(self) -> None:
        provider = self._provider()
        for moment in (
            vienna(2026, 11, 3, 5, 59),   # before
            vienna(2026, 11, 3, 10, 0),   # end is exclusive
            vienna(2026, 11, 7, 7, 0),    # Saturday
            vienna(2026, 11, 1, 7, 0),    # Sunday
        ):
            with self.subTest(moment=moment):
                self.clock.set_wall(moment.astimezone(timezone.utc))
                self.assertEqual(provider.status(), {
                    "status": "inactive", "updated_at": None, "message": "", "data": None,
                })
        self.assertEqual(self.http.calls, [])
        self.clock.set_wall(vienna(2026, 11, 3, 6, 0).astimezone(timezone.utc))
        self.assertTrue(any(provider.open_stops(self.clock.now())))

    def test_requests_within_the_cache_share_one_fetch(self) -> None:
        self._answer_both()
        provider = self._provider()
        provider.status()
        self.clock.advance(CACHE_TTL_SECONDS - 1)
        provider.status()
        self.assertEqual(len(self.http.calls), 2)  # one per stop
        self.clock.advance(1)
        provider.status()
        self.assertEqual(len(self.http.calls), 4)

    def test_one_failing_stop_leaves_the_other_alone(self) -> None:
        self.http.add_error("GET", URL_70, HttpError("timed out"))
        self.http.add_response("GET", URL_46, body=HAFENPORTAL)
        result = self._provider().status()
        self.assertEqual(result["status"], "stale")
        failed, fine = result["data"]["stops"]
        self.assertEqual((failed["status"], failed["departures"], failed["updated_at"]), ("error", [], None))
        self.assertEqual(fine["status"], "ok")
        self.assertEqual(result["updated_at"], fine["updated_at"])

    def test_stale_keeps_last_good_departures_then_gives_up(self) -> None:
        self.http.queue("GET", URL_70, body=SCHIFFSWERFT)
        self.http.add_response("GET", URL_46, body=HAFENPORTAL)
        provider = self._provider()
        provider.status()
        self.http.add_error("GET", URL_70, HttpError("timed out"))

        self.clock.advance(STALE_MAX_AGE_SECONDS)
        stale = provider.status()["data"]["stops"][0]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["updated_at"], "2026-11-03T06:00:00+00:00")
        self.assertEqual(times(stale["departures"]), ["07:06", "07:26", "07:49"])

        self.clock.advance(CACHE_TTL_SECONDS)
        gone = provider.status()["data"]["stops"][0]
        self.assertEqual((gone["status"], gone["departures"]), ("error", []))
        self.assertEqual(gone["updated_at"], "2026-11-03T06:00:00+00:00")

    def test_garbage_and_http_errors_are_errors(self) -> None:
        for kwargs in ({"body": b"<html>Wartung</html>"}, {"body": {"x": 1}}, {"status": 503, "body": b""}):
            with self.subTest(kwargs=kwargs):
                self.http = FakeHttpClient()
                self.http.add_response("GET", URL_70, **kwargs)
                self.http.add_response("GET", URL_46, **kwargs)
                result = self._provider().status()
                self.assertEqual(result["status"], "error")
                self.assertEqual([s["status"] for s in result["data"]["stops"]], ["error", "error"])

    def test_a_full_list_without_the_line_is_not_knowing_not_empty(self) -> None:
        other = HAFENPORTAL["departureList"][0]
        crowded = {"departureList": [
            {**other, "servingLine": {**other["servingLine"], "number": "27"}}
        ] * DM_LIMIT}
        self.http.add_response("GET", URL_70, body=crowded)
        self.http.add_response("GET", URL_46, body={"departureList": None})
        first, second = self._provider().status()["data"]["stops"]
        self.assertEqual((first["status"], first["departures"]), ("error", []))
        # A short list without the line is a real "keine Abfahrt".
        self.assertEqual((second["status"], second["departures"]), ("ok", []))

    def test_rate_limit_pauses_that_stop_only(self) -> None:
        self.http.add_response("GET", URL_70, status=429, body=b"", headers={"Retry-After": "600"})
        self.http.add_response("GET", URL_46, body=HAFENPORTAL)
        provider = self._provider()
        provider.status()
        for _ in range(3):
            self.clock.advance(CACHE_TTL_SECONDS)
            result = provider.status()
        self.assertEqual(len(self.http.calls_matching("GET", URL_70)), 1)
        self.assertEqual(len(self.http.calls_matching("GET", URL_46)), 4)
        self.assertEqual([s["status"] for s in result["data"]["stops"]], ["error", "ok"])
        self.clock.advance(600)
        provider.status()
        self.assertEqual(len(self.http.calls_matching("GET", URL_70)), 2)

    def test_an_unexpected_exception_stays_with_its_stop(self) -> None:
        # Something no handler names (not FetchError, not ValueError) for
        # the first stop only.
        class _Exploding(FakeHttpClient):
            def get(self, url, **kwargs):
                if url == URL_70:
                    raise RuntimeError("boom")
                return super().get(url, **kwargs)

        self.http = _Exploding()
        self.http.add_response("GET", URL_46, body=HAFENPORTAL)
        result = self._provider().status()
        self.assertEqual([s["status"] for s in result["data"]["stops"]], ["error", "ok"])

    def test_walk_minutes_reach_the_frontend(self) -> None:
        self._answer_both()
        stops = [replace(STOPS[0], walk_minutes=7), STOPS[1]]
        config = replace(self.config, transit=TransitConfig(stops=stops))
        first = self._provider(config).status()["data"]["stops"][0]
        self.assertEqual(first["walk_minutes"], 7)
        self.assertEqual(times(first["departures"]), ["07:26", "07:49"])

    def test_disabled_without_stops(self) -> None:
        provider = self._provider(replace(self.config, transit=TransitConfig()))
        self.assertFalse(provider.enabled)
        self.assertIsNone(provider.ui_config())
        self.assertEqual(provider.status()["status"], "disabled")
        self.assertEqual(self.http.calls, [])

    def test_demo_mode_without_stops(self) -> None:
        config = replace(
            self.config, transit=TransitConfig(), app=replace(self.config.app, demo_mode=True)
        )
        provider = self._provider(config)
        self.assertEqual([s["label"] for s in provider.ui_config()["stops"]], ["Schiffswerft", "Hafenportal"])
        result = provider.status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(times(result["data"]["stops"][0]["departures"])[:3], ["07:06", "07:26", "07:49"])
        self.assertEqual(self.http.calls, [])

    def test_ui_config(self) -> None:
        stops = [replace(STOPS[0], window_from="06:00", window_to="08:30"), STOPS[1]]
        config = replace(self.config, transit=TransitConfig(stops=stops))
        self.assertEqual(self._provider(config).ui_config(), {
            "stops": [
                {"line": "70", "label": "Schiffswerft", "from": "06:00", "to": "08:30"},
                {"line": "46", "label": "Hafenportal", "from": "06:00", "to": "10:00"},
            ],
            "days": [1, 2, 3, 4, 5],
            "departures": 3,
        })


class PerStopWindowTest(unittest.TestCase):
    """#28: the 70 runs only in the morning, the 46 all day."""

    # Borrowed, not inherited: a subclass would re-run every test above with
    # this config.
    _provider = TransitProviderTest._provider
    _answer_both = TransitProviderTest._answer_both

    def setUp(self) -> None:
        TransitProviderTest.setUp(self)
        stops = [replace(STOPS[0], window_from="06:00", window_to="07:30"), STOPS[1]]
        self.config = replace(self.config, transit=TransitConfig(stops=stops))

    def test_a_closed_stop_is_kept_in_place_and_not_fetched(self) -> None:
        self.clock.set_wall(vienna(2026, 11, 3, 8, 0).astimezone(timezone.utc))
        self.http.add_response("GET", URL_46, body=HAFENPORTAL)
        provider = self._provider()
        self.assertEqual(provider.open_stops(self.clock.now()), [False, True])
        self.assertTrue(any(provider.open_stops(self.clock.now())))
        result = provider.status()
        self.assertEqual(result["status"], "ok")
        first, second = result["data"]["stops"]
        self.assertEqual(first, {
            "line": "70", "label": "Schiffswerft", "walk_minutes": 0,
            "status": "inactive", "updated_at": None, "departures": [],
        })
        self.assertEqual((second["status"], times(second["departures"])), ("ok", ["08:12", "08:27"]))
        self.assertEqual([call.url for call in self.http.calls], [URL_46])

    def test_a_window_closing_is_not_answered_from_the_cache(self) -> None:
        self.clock.set_wall(vienna(2026, 11, 3, 7, 29, 50).astimezone(timezone.utc))
        self._answer_both()
        provider = self._provider()
        self.assertEqual(provider.status()["data"]["stops"][0]["status"], "ok")
        self.clock.advance(15)  # 07:30:05, well inside CACHE_TTL_SECONDS
        stops = provider.status()["data"]["stops"]
        self.assertEqual([s["status"] for s in stops], ["inactive", "ok"])
        self.assertEqual([call.url for call in self.http.calls], [URL_70, URL_46, URL_46])

    def test_all_windows_closed_is_inactive(self) -> None:
        self.clock.set_wall(vienna(2026, 11, 3, 10, 0).astimezone(timezone.utc))
        provider = self._provider()
        self.assertEqual(provider.open_stops(self.clock.now()), [False, False])
        self.assertEqual(provider.status()["status"], "inactive")
        self.assertEqual(self.http.calls, [])

    def test_a_failure_of_the_only_open_stop_is_its_own(self) -> None:
        self.clock.set_wall(vienna(2026, 11, 3, 8, 0).astimezone(timezone.utc))
        self.http.add_error("GET", URL_46, HttpError("timed out"))
        result = self._provider().status()
        self.assertEqual(result["status"], "error")
        self.assertEqual([s["status"] for s in result["data"]["stops"]], ["inactive", "error"])

    def test_days_still_apply_to_every_stop(self) -> None:
        self.clock.set_wall(vienna(2026, 11, 7, 7, 0).astimezone(timezone.utc))  # Saturday
        self.assertEqual(self._provider().open_stops(self.clock.now()), [False, False])


class _StubTransit:
    enabled = True

    def __init__(self, open_stops: list[bool] | None = None) -> None:
        self.calls = 0
        self._open = [True] if open_stops is None else open_stops

    def open_stops(self, now) -> list[bool]:
        return list(self._open)

    def ui_config(self):
        stops = [{"line": "70", "label": "Schiffswerft", "from": "06:00", "to": "08:30"},
                 {"line": "46", "label": "Hafenportal", "from": "05:00", "to": "23:59"}]
        return {"stops": stops[: len(self._open)], "days": [1, 2, 3, 4, 5], "departures": 3}

    def status(self):
        self.calls += 1
        return {"status": "ok", "updated_at": None, "message": "", "data": {"stops": []}}


class TransitRouteTest(unittest.TestCase):
    def _client(self, services_extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = Flask(__name__)
        app.extensions["smart_display"] = {
            "config": make_app_config(Path(tmp.name)),
            "state_store": make_state_store(Path(tmp.name)),
            **services_extra,
        }
        app.register_blueprint(create_blueprint())
        return app.test_client()

    def test_route_is_local_only(self) -> None:
        stub = _StubTransit()
        client = self._client({"transit": stub})
        ok = client.get("/api/transit", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(ok.get_json()["data"], {"stops": []})
        rejected = client.get("/api/transit", environ_overrides={"REMOTE_ADDR": "192.168.178.40"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(stub.calls, 1)

    def test_route_without_provider_is_disabled(self) -> None:
        response = self._client({}).get("/api/transit", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(response.get_json()["status"], "disabled")

    def test_page_renders_the_rows_and_reserves_them_inside_the_window(self) -> None:
        for services, rows, hidden, flag in (
            ({}, 0, 0, "off"),
            ({"transit": _StubTransit([False])}, 1, 1, "off"),
            ({"transit": _StubTransit([True])}, 1, 0, "on"),
            # #28: the 70 outside its own window, the 46 inside the global one.
            ({"transit": _StubTransit([False, True])}, 2, 1, "on"),
        ):
            with self.subTest(services=services, flag=flag):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                app = make_blueprint_app(Path(tmp.name))
                app.extensions["smart_display"].update(services)
                html = app.test_client().get("/").get_data(as_text=True)
                self.assertIn(f'data-transit="{flag}"', html)
                self.assertEqual(html.count('class="transit__row"'), rows)
                self.assertEqual(html.count('class="transit__row" hidden'), hidden)
                self.assertIn('id="transit"', html)


class TransitConfigTest(unittest.TestCase):
    def _load(self, env: dict) -> TransitConfig:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config" / "default.yaml").write_text("{}", encoding="utf-8")
            return load_config(env=env, dotenv_path=None, root_dir=root).transit

    def test_default_is_off(self) -> None:
        self.assertEqual(self._load({}), TransitConfig())
        self.assertEqual(TransitConfig().stops, [])

    def test_env(self) -> None:
        config = self._load({
            "TRANSIT_STOPS": " 60501350:70:Schiffswerft , 60500097:46:Hafenportal:4 ",
            "TRANSIT_WINDOW": "6:30-9:45",
            "TRANSIT_DAYS": "1-4,6",
            "TRANSIT_DEPARTURES": "2",
        })
        self.assertEqual(config.stops, [
            TransitStop("60501350", "70", "Schiffswerft", 0),
            TransitStop("60500097", "46", "Hafenportal", 4),
        ])
        self.assertEqual((config.window_from, config.window_to), ("06:30", "09:45"))
        self.assertEqual(config.days, [1, 2, 3, 4, 6])
        self.assertEqual(config.departures, 2)

    def test_a_stop_may_have_its_own_window(self) -> None:
        config = self._load({
            "TRANSIT_STOPS": "60501350:70:Schiffswerft@6:00-8:30,60500097:46:Hafenportal:3",
            "TRANSIT_WINDOW": "05:00-23:59",
        })
        self.assertEqual(config.stops, [
            TransitStop("60501350", "70", "Schiffswerft", 0, "06:00", "08:30"),
            TransitStop("60500097", "46", "Hafenportal", 3, None, None),
        ])
        self.assertEqual((config.window_from, config.window_to), ("05:00", "23:59"))
        walk_and_window = self._load({"TRANSIT_STOPS": "60501350:70:Schiffswerft:2@06:00-08:30"})
        self.assertEqual(walk_and_window.stops, [
            TransitStop("60501350", "70", "Schiffswerft", 2, "06:00", "08:30"),
        ])

    def test_a_garbled_own_window_drops_only_its_stop(self) -> None:
        for window in ("@08:30-06:00", "@morgens", "@", "@06:00-08:30@09:00-10:00", "@06:00-24:00"):
            with self.subTest(window=window):
                config = self._load({
                    "TRANSIT_STOPS": f"60501350:70:Schiffswerft{window},60500097:46:Hafenportal",
                })
                self.assertEqual(config.stops, [TransitStop("60500097", "46", "Hafenportal")])

    def test_yaml_stop_with_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config" / "default.yaml").write_text(json.dumps({"transit": {"stops": [
                {"stop_id": "60501350", "line": "70", "label": "Schiffswerft", "window": "06:00-08:30"},
                {"stop_id": "60500097", "line": "46", "label": "Hafenportal"},
            ]}}), encoding="utf-8")
            stops = load_config(env={}, dotenv_path=None, root_dir=root).transit.stops
        self.assertEqual([(s.window_from, s.window_to) for s in stops], [("06:00", "08:30"), (None, None)])

    def test_garbled_stops_are_dropped(self) -> None:
        config = self._load({"TRANSIT_STOPS": "abc:70:X,60501350:70,60501350::X,60501350:70:X:lots,60500097:46:Hafenportal"})
        self.assertEqual(config.stops, [TransitStop("60500097", "46", "Hafenportal", 0)])

    def test_a_garbled_window_or_day_list_switches_off_instead_of_guessing(self) -> None:
        stops = "60500097:46:Hafenportal"
        for env in (
            {"TRANSIT_WINDOW": "10:00-06:00"},
            {"TRANSIT_WINDOW": "morgens"},
            {"TRANSIT_WINDOW": "06:00-25:00"},
            {"TRANSIT_DAYS": "1-8"},
            {"TRANSIT_DAYS": "Mo-Fr"},
        ):
            with self.subTest(env=env):
                self.assertEqual(self._load({"TRANSIT_STOPS": stops, **env}).stops, [])

    def test_empty_window_or_days_mean_the_default(self) -> None:
        config = self._load({
            "TRANSIT_STOPS": "60500097:46:Hafenportal", "TRANSIT_WINDOW": "", "TRANSIT_DAYS": "",
        })
        self.assertEqual(len(config.stops), 1)
        self.assertEqual((config.window_from, config.window_to, config.days),
                         ("06:00", "10:00", [1, 2, 3, 4, 5]))

    def test_timeout_is_capped_so_an_outage_cannot_hold_the_lock_long(self) -> None:
        self.assertEqual(self._load({"TRANSIT_TIMEOUT_SECONDS": "30"}).timeout_seconds, 4)
        self.assertEqual(self._load({"TRANSIT_TIMEOUT_SECONDS": "5"}).timeout_seconds, 5)

    def test_out_of_range_numbers_fall_back(self) -> None:
        config = self._load({"TRANSIT_DEPARTURES": "0", "TRANSIT_TIMEOUT_SECONDS": "soon"})
        self.assertEqual((config.departures, config.timeout_seconds), (3, 4))


if __name__ == "__main__":
    unittest.main()
