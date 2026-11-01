from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from flask import Flask

from smart_display.config import Pi5StatusConfig, load_config
from smart_display.http_client import HttpError
from smart_display.providers.pi5_status import (
    CACHE_TTL_SECONDS,
    STALE_MAX_AGE_SECONDS,
    Pi5StatusProvider,
    normalize_status,
)
from smart_display.web.routes import create_blueprint
from tests._support import (
    FakeClock,
    FakeHttpClient,
    make_app_config,
    make_blueprint_app,
    make_state_store,
)

URL = "http://192.168.178.199:8787/"

# A real `pistatus --json` answer from the pi5 (2026-11-01).
PISTATUS = {
    "host": "pi5", "now": 1793564901, "boot": 1793562673,
    "temp_c": 49.9, "temp_level": "ok", "fan_rpm": 0,
    "throttled": "0x0", "throttled_level": "ok",
    "ram": {"used": 630554624, "total": 17006116864, "pct": 4, "level": "ok"},
    "disk": {"used": 6347919360, "total": 62256185344, "pct": 11, "level": "ok"},
    "backup": {"state": "ok", "epoch": 1793563587, "result": None},
    "update": {"state": "unknown", "epoch": 1792986308, "result": None},
    "containers": [
        {"name": "pihole", "state": "running", "health": "healthy"},
        {"name": "spotipi", "state": "running", "health": "unhealthy"},
    ],
}


class NormalizeStatusTest(unittest.TestCase):
    def test_real_answer_passes_through(self) -> None:
        data = normalize_status(PISTATUS)
        self.assertEqual(data["temp_c"], 49.9)
        self.assertEqual(data["ram"], PISTATUS["ram"])
        self.assertEqual(data["backup"], PISTATUS["backup"])
        self.assertEqual(data["containers"][1]["health"], "unhealthy")

    def test_not_a_pistatus_answer(self) -> None:
        for payload in (None, [], "ok", {"status": "fine"}):
            self.assertIsNone(normalize_status(payload), payload)

    def test_unknown_stays_unknown(self) -> None:
        data = normalize_status({
            "temp_level": "purple", "temp_c": None, "fan_rpm": None, "throttled": None,
            "ram": {"used": "lots", "total": 0, "pct": 140, "level": "ok"},
            "backup": {"state": "exploded", "epoch": -5},
            "containers": [{"name": "x", "state": "zombie", "health": "meh"}, "junk"],
        })
        self.assertEqual(data["temp_level"], "unknown")
        self.assertIsNone(data["temp_c"])
        self.assertIsNone(data["fan_rpm"])  # not 0: "aus" would be a lie
        self.assertEqual(data["ram"], {"used": None, "total": None, "pct": None, "level": "ok"})
        self.assertEqual(data["disk"]["level"], "unknown")
        self.assertEqual(data["backup"], {"state": "unknown", "epoch": None, "result": None})
        self.assertEqual(data["containers"], [{"name": "x", "state": "unknown", "health": "none"}])

    def test_docker_silent_is_not_no_containers(self) -> None:
        self.assertIsNone(normalize_status({**PISTATUS, "containers": None})["containers"])
        self.assertEqual(normalize_status({**PISTATUS, "containers": []})["containers"], [])

    def test_booleans_are_not_numbers(self) -> None:
        self.assertIsNone(normalize_status({**PISTATUS, "temp_c": True})["temp_c"])


class Pi5StatusProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()
        self.http = FakeHttpClient()
        base = make_app_config(Path(self._tmp.name))
        self.config = replace(base, pi5_status=Pi5StatusConfig(url=URL, timeout_seconds=2))

    def _provider(self, config=None) -> Pi5StatusProvider:
        return Pi5StatusProvider(
            config or self.config,
            http_client=self.http,
            monotonic=self.clock.monotonic,
            utcnow=self.clock.now,
        )

    def test_ok(self) -> None:
        self.http.add_response("GET", URL, body=PISTATUS)
        result = self._provider().status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["updated_at"], "2026-04-08T12:00:00+00:00")
        self.assertEqual(result["data"]["host"], "pi5")
        call = self.http.calls[0]
        self.assertEqual((call.timeout, call.max_bytes), (2.0, 16_000))

    def test_requests_within_the_window_share_one_fetch(self) -> None:
        self.http.add_response("GET", URL, body=PISTATUS)
        provider = self._provider()
        provider.status()
        self.clock.advance(CACHE_TTL_SECONDS - 1)
        provider.status()
        self.assertEqual(len(self.http.calls), 1)
        self.clock.advance(1)
        provider.status()
        self.assertEqual(len(self.http.calls), 2)

    def test_failures_are_shared_too(self) -> None:
        self.http.add_error("GET", URL, HttpError("timed out"))
        provider = self._provider()
        provider.status()
        provider.status()
        self.assertEqual(len(self.http.calls), 1)

    def test_error_without_good_data(self) -> None:
        self.http.add_error("GET", URL, HttpError("timed out"))
        result = self._provider().status()
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["data"])
        self.assertIsNone(result["updated_at"])
        self.assertIn("pi5 nicht erreichbar", result["message"])

    def test_stale_keeps_last_good_data_with_its_age_then_gives_up(self) -> None:
        self.http.queue("GET", URL, body=PISTATUS)
        provider = self._provider()
        provider.status()
        self.http.add_error("GET", URL, HttpError("timed out"))

        self.clock.advance(STALE_MAX_AGE_SECONDS)
        stale = provider.status()
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["data"]["temp_c"], 49.9)
        self.assertEqual(stale["updated_at"], "2026-04-08T12:00:00+00:00")

        self.clock.advance(CACHE_TTL_SECONDS)
        gone = provider.status()
        self.assertEqual(gone["status"], "error")
        self.assertIsNone(gone["data"])
        self.assertEqual(gone["updated_at"], "2026-04-08T12:00:00+00:00")

    def test_garbage_answers_are_errors(self) -> None:
        for body in (b"<html>pihole</html>", {"status": "fine"}):
            with self.subTest(body=body):
                self.http = FakeHttpClient()
                self.http.add_response("GET", URL, body=body)
                self.assertEqual(self._provider().status()["status"], "error")

    def test_unparsable_url_is_an_error_not_a_crash(self) -> None:
        class _BadPortClient:
            def get(self, url, **kwargs):
                raise ValueError("Port could not be cast to integer value as '87a7'")

        provider = Pi5StatusProvider(
            self.config, http_client=_BadPortClient(), monotonic=self.clock.monotonic, utcnow=self.clock.now
        )
        result = provider.status()
        self.assertEqual(result["status"], "error")
        self.assertIn("87a7", result["message"])

    def test_http_error_status(self) -> None:
        self.http.add_response("GET", URL, status=503, body=b"")
        self.assertEqual(self._provider().status()["status"], "error")

    def test_disabled_without_url(self) -> None:
        config = replace(self.config, pi5_status=Pi5StatusConfig())
        provider = self._provider(config)
        self.assertFalse(provider.enabled)
        self.assertEqual(provider.status()["status"], "disabled")
        self.assertEqual(self.http.calls, [])

    def test_demo_mode_without_url_shows_demo_data(self) -> None:
        config = replace(
            self.config,
            pi5_status=Pi5StatusConfig(),
            app=replace(self.config.app, demo_mode=True),
        )
        provider = self._provider(config)
        self.assertTrue(provider.enabled)
        result = provider.status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["boot"], result["data"]["now"] - 19 * 60)
        self.assertEqual(self.http.calls, [])


class _StubPi5:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def status(self):
        self.calls += 1
        return {"status": "ok", "updated_at": None, "message": "", "data": {"host": "pi5"}}


class Pi5RouteTest(unittest.TestCase):
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
        stub = _StubPi5()
        client = self._client({"pi5_status": stub})
        ok = client.get("/api/pi5", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["data"], {"host": "pi5"})
        rejected = client.get("/api/pi5", environ_overrides={"REMOTE_ADDR": "192.168.178.40"})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(stub.calls, 1)

    def test_route_without_provider_is_disabled(self) -> None:
        client = self._client({})
        response = client.get("/api/pi5", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(response.get_json()["status"], "disabled")

    def test_page_tells_the_frontend_whether_the_screen_exists(self) -> None:
        # app.js builds SCREEN_ORDER from this flag; without it nothing pages to pi5.
        for services, expected in (({}, False), ({"pi5_status": _StubPi5()}, True)):
            with self.subTest(expected=expected):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                app = make_blueprint_app(Path(tmp.name))
                app.extensions["smart_display"].update(services)
                html = app.test_client().get("/").get_data(as_text=True)
                self.assertIn(f'"pi5_enabled": {"true" if expected else "false"}', html)


class Pi5ConfigTest(unittest.TestCase):
    def test_env_overrides_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config" / "default.yaml").write_text("{}", encoding="utf-8")
            default = load_config(env={}, dotenv_path=None, root_dir=root)
            self.assertEqual(default.pi5_status, Pi5StatusConfig(url="", timeout_seconds=2))
            config = load_config(
                env={"PI5_STATUS_URL": f" {URL} ", "PI5_STATUS_TIMEOUT_SECONDS": "3"},
                dotenv_path=None,
                root_dir=root,
            )
            self.assertEqual(config.pi5_status, Pi5StatusConfig(url=URL, timeout_seconds=3))

    def test_a_broken_timeout_never_stops_the_app(self) -> None:
        # The pi5 screen is optional: "PI5_STATUS_TIMEOUT_SECONDS=" must not
        # keep smart-display.service from starting.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config" / "default.yaml").write_text('{"pi5_status": null}', encoding="utf-8")
            nulled = load_config(env={}, dotenv_path=None, root_dir=root)
            self.assertEqual(nulled.pi5_status, Pi5StatusConfig())
            (root / "config" / "default.yaml").write_text("{}", encoding="utf-8")
            for raw in ("", "soon", "0", "-3"):
                with self.subTest(raw=raw):
                    config = load_config(
                        env={"PI5_STATUS_TIMEOUT_SECONDS": raw}, dotenv_path=None, root_dir=root
                    )
                    self.assertEqual(config.pi5_status.timeout_seconds, 2)


if __name__ == "__main__":
    unittest.main()
