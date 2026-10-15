from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from smart_display.models import DashboardState, TimerState
from smart_display.timers import (
    MAX_TIMERS,
    RING_TIMEOUT_SECONDS,
    TimerService,
    timer_status,
)
from tests._support import FakeClock, make_app_config, make_state_store


def _service(tmp: str, clock: FakeClock) -> tuple[TimerService, FakeClock]:
    store = make_state_store(Path(tmp))
    ids = iter(f"t{n}" for n in range(1, 100))
    return TimerService(store, clock=clock.monotonic, id_factory=lambda: next(ids)), clock


class TimerStatusTest(unittest.TestCase):
    def test_running_then_ringing_then_done(self) -> None:
        timer = TimerState(id="a", label="", duration_seconds=60, ends_at=1060.0, created_at=1000.0)
        self.assertEqual(timer_status(timer, 1059.9), "running")
        self.assertEqual(timer_status(timer, 1060.0), "ringing")
        self.assertEqual(timer_status(timer, 1060.0 + RING_TIMEOUT_SECONDS - 1), "ringing")
        self.assertEqual(timer_status(timer, 1060.0 + RING_TIMEOUT_SECONDS), "done")

    def test_dismissed_is_done_even_while_running(self) -> None:
        timer = TimerState(id="a", label="", duration_seconds=60, ends_at=1060.0, created_at=1000.0, dismissed=True)
        self.assertEqual(timer_status(timer, 1000.0), "done")


class TimerServiceTest(unittest.TestCase):
    def test_create_lists_running_timer_with_remaining_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, clock = _service(tmp, FakeClock(monotonic=1000.0))
            result = service.create(300, "Nudeln")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["timers"]), 1)
            timer = result["timers"][0]
            self.assertEqual(timer["id"], "t1")
            self.assertEqual(timer["label"], "Nudeln")
            self.assertEqual(timer["status"], "running")
            self.assertEqual(timer["remaining_seconds"], 300)
            clock.advance(120)
            self.assertEqual(service.list()[0]["remaining_seconds"], 180)

    def test_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _service(tmp, FakeClock(monotonic=1000.0))
            self.assertFalse(service.create("abc")["ok"])
            self.assertFalse(service.create(5)["ok"])
            self.assertFalse(service.create(24 * 3600 + 1)["ok"])
            self.assertTrue(service.create(10)["ok"])

    def test_max_timers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, _ = _service(tmp, FakeClock(monotonic=1000.0))
            for _ in range(MAX_TIMERS):
                self.assertTrue(service.create(60)["ok"])
            result = service.create(60)
            self.assertFalse(result["ok"])
            self.assertIn(str(MAX_TIMERS), result["message"])

    def test_cancel_and_dismiss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, clock = _service(tmp, FakeClock(monotonic=1000.0))
            service.create(60)
            service.create(120)
            self.assertTrue(service.cancel("t1")["ok"])
            self.assertEqual([t["id"] for t in service.list()], ["t2"])
            self.assertFalse(service.cancel("t1")["ok"])

            clock.advance(130)
            self.assertEqual(service.list()[0]["status"], "ringing")
            self.assertTrue(service.dismiss("t2")["ok"])
            self.assertEqual(service.list(), [])

    def test_ringing_times_out_without_dismiss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, clock = _service(tmp, FakeClock(monotonic=1000.0))
            service.create(60)
            clock.advance(60 + RING_TIMEOUT_SECONDS)
            self.assertEqual(service.list(), [])
            # The next mutation prunes it from the store, so a slot frees up.
            for _ in range(MAX_TIMERS):
                self.assertTrue(service.create(60)["ok"])

    def test_extend_running_and_ringing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service, clock = _service(tmp, FakeClock(monotonic=1000.0))
            service.create(60)
            self.assertTrue(service.extend("t1", 60)["ok"])
            self.assertEqual(service.list()[0]["remaining_seconds"], 120)
            clock.advance(130)  # now ringing for 10 s
            self.assertEqual(service.list()[0]["status"], "ringing")
            self.assertTrue(service.extend("t1", 60)["ok"])
            timer = service.list()[0]
            self.assertEqual(timer["status"], "running")
            self.assertEqual(timer["remaining_seconds"], 60)
            self.assertFalse(service.extend("t1", 0)["ok"])
            self.assertFalse(service.extend("nope", 60)["ok"])

    def test_timers_survive_a_store_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeClock(monotonic=1000.0)
            service, _ = _service(tmp, clock)
            service.create(600, "Wäsche")
            config = make_app_config(Path(tmp))
            payload = json.loads(config.last_good_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["timers"][0]["label"], "Wäsche")

            reloaded_store = make_state_store(Path(tmp))
            reloaded = TimerService(reloaded_store, clock=clock.monotonic)
            self.assertEqual(reloaded.list()[0]["remaining_seconds"], 600)

    def test_old_last_good_without_timers_still_loads(self) -> None:
        payload = DashboardState.from_dict({}).to_dict()
        payload.pop("timers")
        state = DashboardState.from_dict(payload)
        self.assertEqual(state.timers, [])


if __name__ == "__main__":
    unittest.main()
