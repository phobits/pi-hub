from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from smart_display.power import NOT_ALLOWED_MESSAGE, PowerController
from smart_display.timers import TimerService
from tests._support import FakeClock, make_blueprint_app


def _app(tmp: str, *, allowed: bool, runner=None, time_synced=True):
    app = make_blueprint_app(Path(tmp))
    services = app.extensions["smart_display"]
    clock = FakeClock(monotonic=1_700_000_000.0)
    services["timer_service"] = TimerService(services["state_store"], clock=clock.monotonic)
    services["power_controller"] = PowerController(allowed=allowed, runner=runner)
    services["time_sync_probe"] = lambda: time_synced
    return app, clock


class PowerRoutesTest(unittest.TestCase):
    def test_shutdown_refused_when_not_allowed(self) -> None:
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = _app(tmp, allowed=False, runner=lambda argv: calls.append(tuple(argv)) or 0)
            response = app.test_client().post("/api/system/shutdown")
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json()["message"], NOT_ALLOWED_MESSAGE)
            self.assertEqual(calls, [])

    def test_shutdown_runs_fixed_argv_when_allowed(self) -> None:
        calls: list[tuple[str, ...]] = []
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = _app(tmp, allowed=True, runner=lambda argv: calls.append(tuple(argv)) or 0)
            response = app.test_client().post("/api/system/shutdown")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(calls, [("sudo", "-n", "/usr/bin/systemctl", "poweroff")])

            response = app.test_client().post("/api/system/reboot")
            self.assertEqual(calls[-1], ("sudo", "-n", "/usr/bin/systemctl", "reboot"))

    def test_nonzero_exit_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = _app(tmp, allowed=True, runner=lambda argv: 1)
            response = app.test_client().post("/api/system/shutdown")
            self.assertEqual(response.status_code, 403)
            self.assertFalse(response.get_json()["ok"])

    def test_remote_origin_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = _app(tmp, allowed=True, runner=lambda argv: 0)
            response = app.test_client().post(
                "/api/system/shutdown", headers={"Origin": "http://evil.example"}
            )
            self.assertEqual(response.status_code, 403)


class TimerRoutesTest(unittest.TestCase):
    def test_create_cancel_extend_dismiss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, clock = _app(tmp, allowed=False)
            client = app.test_client()
            response = client.post("/api/timers", json={"duration_seconds": 300, "label": "Tee"})
            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            timer_id = body["timers"][0]["id"]
            self.assertEqual(body["timers"][0]["remaining_seconds"], 300)

            # /api/state carries the live view with status + remaining.
            state = client.get("/api/state").get_json()
            self.assertEqual(state["timers"][0]["status"], "running")
            self.assertTrue(state["system"]["time_synced"])

            self.assertEqual(client.post(f"/api/timers/{timer_id}/extend", json={"seconds": 60}).status_code, 200)
            self.assertEqual(client.get("/api/state").get_json()["timers"][0]["remaining_seconds"], 360)

            clock.advance(400)
            self.assertEqual(client.get("/api/state").get_json()["timers"][0]["status"], "ringing")
            self.assertEqual(client.post(f"/api/timers/{timer_id}/dismiss").status_code, 200)
            self.assertEqual(client.get("/api/state").get_json()["timers"], [])

            self.assertEqual(client.post(f"/api/timers/{timer_id}/cancel").status_code, 404)
            # #4: every timer route says 404 for an unknown id, 400 for bad input.
            self.assertEqual(client.post(f"/api/timers/{timer_id}/extend", json={"seconds": 60}).status_code, 404)
            self.assertEqual(client.post(f"/api/timers/{timer_id}/dismiss").status_code, 404)
            self.assertEqual(client.post("/api/timers", json={"duration_seconds": 1}).status_code, 400)

    def test_index_carries_night_and_shutdown_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, _ = _app(tmp, allowed=False, time_synced=False)
            html = app.test_client().get("/").get_data(as_text=True)
            self.assertIn('"night": {"from": "22:30", "to": "06:00"}', html)
            self.assertIn('"allow_shutdown": false', html)
            self.assertIn('"time_synced": false', html)
            self.assertIn('data-night="', html)


if __name__ == "__main__":
    unittest.main()
