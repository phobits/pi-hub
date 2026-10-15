from __future__ import annotations

import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

from flask import Flask

from smart_display.app import serve_app
from smart_display.config import load_config
from smart_display.local_server import _load_local_env
from smart_display.web.routes import _poll_interval_seconds, create_blueprint
from tests._support import make_app_config, make_state_store


class LocalServerTest(unittest.TestCase):
    def test_local_demo_profile_uses_isolated_demo_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "config").mkdir()
            (root / "config" / "default.yaml").write_text(
                """
{
  "app": {"host": "127.0.0.1", "port": 8080, "locale": "de-AT", "timezone": "Europe/Vienna", "data_dir": "./data", "log_level": "INFO", "demo_mode": false},
  "weather": {"enabled": true, "provider": "openmeteo", "label": "Zuhause", "latitude": 47.0, "longitude": 15.0, "api_key": null, "timeout_seconds": 10},
  "calendar": {"enabled": true, "url": "https://calendar", "username": "u", "password": "p", "calendar_names": [], "timeout_seconds": 10},
  "spotify": {"enabled": true, "client_id": "id", "client_secret": "secret", "refresh_token": "refresh", "device_id": "", "market": "AT", "timeout_seconds": 10},
  "screensaver": {"enabled": true, "idle_timeout_seconds": 120, "image_duration_seconds": 15, "refresh_interval_seconds": 1800, "source_url": "https://gallery", "cache_dir": "screensaver", "demo_images_enabled": true, "timeout_seconds": 15},
  "refresh_intervals": {"weather_seconds": 900, "calendar_seconds": 300, "spotify_seconds": 10, "lightroom_seconds": 1800}
}
                """.strip(),
                encoding="utf-8",
            )
            (root / "config" / "local-demo.yaml").write_text(
                """
{
  "app": {"port": 8090, "data_dir": "./data/local-demo", "demo_mode": true},
  "weather": {"enabled": false},
  "calendar": {"enabled": false},
  "spotify": {"enabled": false}
}
                """.strip(),
                encoding="utf-8",
            )

            config = load_config(
                config_path=root / "config" / "local-demo.yaml",
                env={},
                dotenv_path=None,
                root_dir=root,
            )

            self.assertEqual(config.app.port, 8090)
            self.assertTrue(config.app.demo_mode)
            self.assertFalse(config.weather.enabled)
            self.assertFalse(config.calendar.enabled)
            self.assertFalse(config.spotify.enabled)
            self.assertEqual(config.app.data_dir, (root / "data" / "local-demo").resolve())

    def test_env_local_overrides_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text("APP_PORT=8090\nAPP_HOST=127.0.0.1\n", encoding="utf-8")
            (root / ".env.local").write_text("APP_PORT=8091\n", encoding="utf-8")

            env = _load_local_env(root)

            self.assertEqual(env["APP_PORT"], "8091")
            self.assertEqual(env["APP_HOST"], "127.0.0.1")

    def test_poll_interval_slows_down_without_spotify(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir), spotify_enabled=False)
            config = replace(
                config,
                refresh_intervals=replace(
                    config.refresh_intervals,
                    spotify_seconds=10,
                ),
            )

            self.assertEqual(_poll_interval_seconds(config), 30)

    def test_poll_interval_decoupled_from_backend_when_spotify_enabled(self) -> None:
        # The browser polls the in-process /api/state, which is free, so the
        # frontend cadence is decoupled from the upstream Spotify refresh:
        # capped low for snappy updates, floored against busy-spin.
        with tempfile.TemporaryDirectory() as temp_dir:
            config = make_app_config(Path(temp_dir), spotify_enabled=True)

            real = replace(
                config,
                refresh_intervals=replace(config.refresh_intervals, spotify_seconds=5),
            )
            slow = replace(
                config,
                refresh_intervals=replace(config.refresh_intervals, spotify_seconds=45),
            )
            fast = replace(
                config,
                refresh_intervals=replace(config.refresh_intervals, spotify_seconds=2),
            )

            self.assertEqual(_poll_interval_seconds(real), 4)  # Pi default (backend 5s)
            self.assertEqual(_poll_interval_seconds(slow), 4)  # ceiling
            self.assertEqual(_poll_interval_seconds(fast), 3)  # floor


class _StubScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def set_paused(self, group: str, paused: bool) -> None:
        self.calls.append((group, paused))


class _StubSpotifyProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def toggle_playback(self) -> dict:
        self.calls.append("toggle")
        return {"ok": True, "state": None}

    def next_track(self) -> dict:
        self.calls.append("next")
        return {"ok": True, "state": None}

    def previous_track(self) -> dict:
        self.calls.append("previous")
        return {"ok": True, "state": None}

    def set_volume(self, percent: int) -> dict:
        self.calls.append(f"volume:{percent}")
        return {"ok": True, "state": None}

    def seek_to(self, position_ms: int) -> dict:
        self.calls.append(f"seek:{position_ms}")
        return {"ok": True, "state": None}

    def list_queue(self) -> dict:
        self.calls.append("queue")
        return {"ok": True, "status": "empty", "message": "ok", "items": []}

    def list_devices(self) -> dict:
        self.calls.append("devices")
        return {"ok": True, "message": "ok", "devices": []}

    def list_playlists(self) -> dict:
        self.calls.append("playlists")
        return {"ok": True, "message": "ok", "playlists": []}

    def start_playback(self, device_id: str, context_uri: str | None = None) -> dict:
        self.calls.append(f"play:{device_id}:{context_uri}")
        return {"ok": True, "state": None}


class LoopbackOnlyPostsTest(unittest.TestCase):
    """Plan B10: POST endpoints must only accept loopback callers. The
    kiosk browser talks to 127.0.0.1:8080 so a LAN client or a malicious
    cross-origin page must be rejected even if Waitress is ever mis-bound."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)

        self.config = make_app_config(tmp)
        self.state_store = make_state_store(tmp)
        self.scheduler = _StubScheduler()
        self.spotify = _StubSpotifyProvider()

        app = Flask(
            __name__,
            template_folder=str(
                Path(__file__).parent.parent
                / "smart_display"
                / "web"
                / "templates"
            ),
        )
        app.extensions["smart_display"] = {
            "config": self.config,
            "state_store": self.state_store,
            "scheduler": self.scheduler,
            "spotify_provider": self.spotify,
        }
        app.register_blueprint(create_blueprint())
        self.app = app
        self.client = app.test_client()

    def _post(self, path: str, *, remote: str = "127.0.0.1", **kwargs):
        return self.client.post(
            path,
            environ_overrides={"REMOTE_ADDR": remote},
            **kwargs,
        )

    def test_post_accepts_loopback(self) -> None:
        response = self._post("/api/spotify/toggle")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.spotify.calls, ["toggle"])

    def test_post_rejects_non_loopback_remote_addr(self) -> None:
        response = self._post("/api/spotify/toggle", remote="192.168.1.5")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.spotify.calls, [])
        body = response.get_json()
        self.assertIsNotNone(body)
        self.assertFalse(body["ok"])
        self.assertIn("lokal", body["message"].lower())

    def test_post_rejects_cross_origin_header(self) -> None:
        response = self._post(
            "/api/spotify/toggle",
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.spotify.calls, [])

    def test_post_accepts_loopback_origin_header(self) -> None:
        response = self._post(
            "/api/spotify/toggle",
            headers={"Origin": "http://127.0.0.1:8080"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.spotify.calls, ["toggle"])

    def test_post_accepts_localhost_origin(self) -> None:
        response = self._post(
            "/api/spotify/toggle",
            headers={"Origin": "http://localhost:8080"},
        )
        self.assertEqual(response.status_code, 200)

    def test_post_rejects_cross_origin_referer(self) -> None:
        response = self._post(
            "/api/spotify/next",
            headers={"Referer": "https://attacker.test/page"},
        )
        self.assertEqual(response.status_code, 403)

    def test_screensaver_state_also_guarded(self) -> None:
        response = self._post(
            "/api/screensaver/state",
            json={"active": True},
            remote="10.0.0.4",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.scheduler.calls, [])

    def test_spotify_volume_guarded(self) -> None:
        response = self._post(
            "/api/spotify/volume",
            json={"volume_percent": 50},
            remote="10.0.0.4",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.spotify.calls, [])

    def test_spotify_seek_accepts_integer(self) -> None:
        response = self._post("/api/spotify/seek", json={"position_ms": 42_000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.spotify.calls, ["seek:42000"])

    def test_spotify_seek_rejects_invalid_payloads(self) -> None:
        for payload in ({}, {"position_ms": None}, {"position_ms": -1}, {"position_ms": 3.2}, {"position_ms": True}):
            with self.subTest(payload=payload):
                response = self._post("/api/spotify/seek", json=payload)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.spotify.calls, [])

    def test_spotify_seek_guarded(self) -> None:
        response = self._post(
            "/api/spotify/seek",
            json={"position_ms": 42_000},
            remote="10.0.0.4",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.spotify.calls, [])

    def test_spotify_queue_is_local_only(self) -> None:
        accepted = self.client.get(
            "/api/spotify/queue",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.spotify.calls, ["queue"])

        rejected = self.client.get(
            "/api/spotify/queue",
            environ_overrides={"REMOTE_ADDR": "10.0.0.4"},
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.spotify.calls, ["queue"])

    def test_spotify_play_is_local_only(self) -> None:
        # start_playback can move audio to any speaker in the flat — it is the
        # most abusable write on the blueprint, so pin its guard explicitly.
        rejected = self._post(
            "/api/spotify/play",
            json={"device_id": "box-1"},
            remote="10.0.0.4",
        )
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(self.spotify.calls, [])

        accepted = self._post("/api/spotify/play", json={"device_id": "box-1"})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.spotify.calls, ["play:box-1:None"])

    def test_spotify_list_endpoints_are_local_only(self) -> None:
        for path, expected in (
            ("/api/spotify/devices", "devices"),
            ("/api/spotify/sources", "playlists"),
        ):
            with self.subTest(path=path):
                self.spotify.calls.clear()
                rejected = self.client.get(
                    path, environ_overrides={"REMOTE_ADDR": "10.0.0.4"}
                )
                self.assertEqual(rejected.status_code, 403)
                self.assertEqual(self.spotify.calls, [])

                accepted = self.client.get(
                    path, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}
                )
                self.assertEqual(accepted.status_code, 200)
                self.assertEqual(self.spotify.calls, [expected])

    def test_get_state_unaffected_by_guard(self) -> None:
        # GETs are not guarded at the decorator level — Waitress is already
        # pinned to loopback and the kiosk needs to read state.
        response = self.client.get(
            "/api/state",
            environ_overrides={"REMOTE_ADDR": "192.168.1.5"},
        )
        self.assertEqual(response.status_code, 200)


class _FakeWaitressModule(types.ModuleType):
    """Tiny stand-in so ``serve_app`` can import ``waitress.serve`` during
    tests without actually starting a server. Records the call args so the
    test can assert Waitress was bound to loopback."""

    def __init__(self) -> None:
        super().__init__("waitress")
        self.calls: list[dict] = []

        fake = self

        class _FakeServer:
            def run(self) -> None:
                fake.runs += 1

        def _create_server(app, *, host, port, threads):  # noqa: ANN001 - test stub
            self.calls.append(
                {"app": app, "host": host, "port": port, "threads": threads}
            )
            return _FakeServer()

        self.runs = 0
        self.create_server = _create_server


class ServeAppLoopbackEnforcementTest(unittest.TestCase):
    """Plan B10 (hard fail): ``serve_app`` must refuse a non-loopback host
    at start-up instead of silently coercing it. A silent coercion hides
    the mistake in log noise — a raise forces ops to notice."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._fake = _FakeWaitressModule()
        sys.modules["waitress"] = self._fake
        self.addCleanup(lambda: sys.modules.pop("waitress", None))

    def _make_app(self, host: str) -> Flask:
        tmp = Path(self._tmp.name)
        base = make_app_config(tmp)
        # ``AppConfig``/``AppSection`` are frozen dataclasses in places —
        # ``replace`` is the safe way to swap the host field without
        # depending on mutability.
        config = replace(base, app=replace(base.app, host=host))
        app = Flask(__name__)
        app.extensions["smart_display"] = {"config": config}
        return app

    def test_serve_app_accepts_loopback_host(self) -> None:
        app = self._make_app("127.0.0.1")
        serve_app(app)
        self.assertEqual(len(self._fake.calls), 1)
        self.assertEqual(self._fake.calls[0]["host"], "127.0.0.1")
        # Ticket #1: bind first (create_server), READY=1, then run().
        self.assertEqual(self._fake.runs, 1)
        # Enough headroom that two parallel Spotify proxy calls (the picker
        # fires devices + sources together) cannot starve /api/state.
        self.assertGreaterEqual(self._fake.calls[0]["threads"], 4)

    def test_serve_app_accepts_ipv6_loopback(self) -> None:
        app = self._make_app("::1")
        serve_app(app)
        self.assertEqual(self._fake.calls[0]["host"], "::1")

    def test_serve_app_accepts_localhost(self) -> None:
        app = self._make_app("localhost")
        serve_app(app)
        self.assertEqual(self._fake.calls[0]["host"], "localhost")

    def test_serve_app_rejects_wildcard_bind(self) -> None:
        app = self._make_app("0.0.0.0")
        with self.assertRaises(RuntimeError) as ctx:
            serve_app(app)
        self.assertIn("0.0.0.0", str(ctx.exception))
        self.assertEqual(self._fake.calls, [])

    def test_serve_app_rejects_lan_address(self) -> None:
        app = self._make_app("192.168.1.5")
        with self.assertRaises(RuntimeError):
            serve_app(app)
        self.assertEqual(self._fake.calls, [])


if __name__ == "__main__":
    unittest.main()
