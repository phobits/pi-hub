from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from smart_display.kiosk_watchdog import (
    FRAME_HEADER,
    _run,
    IS_ACTIVE_ARGV,
    REBOOT_MIN_INTERVAL_SECONDS,
    RESTART_GRACE_SECONDS,
    STALE_AFTER_SECONDS,
    KioskWatchdog,
)
from smart_display.power import COMMANDS
from tests._support import FakeClock, make_blueprint_app


REPO = Path(__file__).resolve().parent.parent
WALL_NOW = 1_800_000_000.0


class FakeRunner:
    def __init__(self, kiosk: str = "active") -> None:
        self.kiosk = kiosk
        self.calls: list[tuple[str, ...]] = []
        self.codes: dict[tuple[str, ...], int] = {}
        self.during: dict[tuple[str, ...], object] = {}

    def __call__(self, argv) -> tuple[int, str]:
        argv = tuple(argv)
        self.calls.append(argv)
        if argv in self.during:
            self.during[argv]()
        if argv == IS_ACTIVE_ARGV:
            return (0 if self.kiosk == "active" else 3), self.kiosk
        return self.codes.get(argv, 0), ""

    @property
    def actions(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call != IS_ACTIVE_ARGV]


class KioskWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self._tmp.name) / "kiosk-watchdog.json"
        self.clock = FakeClock(monotonic=1000.0)
        self.wall = WALL_NOW
        self.runner = FakeRunner()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make(self) -> KioskWatchdog:
        return KioskWatchdog(
            state_path=self.state_path,
            runner=self.runner,
            monotonic=self.clock.monotonic,
            wall=lambda: self.wall,
        )

    def test_fresh_frames_never_act(self) -> None:
        watchdog = self.make()
        for _ in range(30):
            self.clock.advance(STALE_AFTER_SECONDS / 3)
            watchdog.note_frame()
            self.assertEqual(watchdog.check(), "ok")
        self.assertEqual(self.runner.calls, [])

    def test_no_frame_since_start_counts_from_start(self) -> None:
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS - 1)
        self.assertEqual(watchdog.check(), "ok")
        self.clock.advance(1)
        self.assertEqual(watchdog.check(), "restarted")
        self.assertEqual(self.runner.actions, [COMMANDS["kiosk_restart"]])

    def test_stale_panel_escalates_restart_then_reboot(self) -> None:
        watchdog = self.make()
        watchdog.note_frame()
        self.clock.advance(STALE_AFTER_SECONDS)
        self.assertEqual(watchdog.check(), "restarted")
        self.clock.advance(RESTART_GRACE_SECONDS - 1)
        self.assertEqual(watchdog.check(), "restarted")
        self.clock.advance(1)
        self.assertEqual(watchdog.check(), "rebooting")
        self.assertEqual(
            self.runner.actions, [COMMANDS["kiosk_restart"], COMMANDS["reboot"]]
        )
        stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["last_reboot"], WALL_NOW)
        # Nothing more while the reboot is under way.
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "rebooting")
        self.assertEqual(len(self.runner.actions), 2)

    def test_frame_after_restart_rearms(self) -> None:
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.clock.advance(60)
        watchdog.note_frame()
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "ok")
        self.assertEqual(self.runner.actions, [COMMANDS["kiosk_restart"]])

    def test_kiosk_stopped_on_purpose_is_left_alone(self) -> None:
        self.runner.kiosk = "inactive"
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS * 4)
        for _ in range(3):
            self.assertEqual(watchdog.check(), "ok")
            self.clock.advance(60)
        self.assertEqual(self.runner.actions, [])

    def test_reboot_held_within_interval_then_allowed(self) -> None:
        self.state_path.write_text(
            json.dumps({"last_reboot": WALL_NOW - REBOOT_MIN_INTERVAL_SECONDS + 600}),
            encoding="utf-8",
        )
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "held")
        self.assertEqual(self.runner.actions, [COMMANDS["kiosk_restart"]])
        self.wall += 599
        self.assertEqual(watchdog.check(), "held")
        self.wall += 1
        self.assertEqual(watchdog.check(), "rebooting")
        self.assertEqual(self.runner.actions[-1], COMMANDS["reboot"])

    def test_clock_behind_stored_reboot_counts_as_recent(self) -> None:
        self.state_path.write_text(json.dumps({"last_reboot": WALL_NOW + 3600}), encoding="utf-8")
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "held")

    def test_unreadable_stamp_allows_reboot(self) -> None:
        self.state_path.write_text("{garbage", encoding="utf-8")
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "rebooting")


    def test_crash_looping_kiosk_still_escalates(self) -> None:
        for state in ("activating", "failed", "deactivating"):
            with self.subTest(state=state):
                self.runner = FakeRunner(kiosk=state)
                self.state_path.unlink(missing_ok=True)
                self.clock = FakeClock(monotonic=1000.0)
                watchdog = self.make()
                self.clock.advance(STALE_AFTER_SECONDS)
                self.assertEqual(watchdog.check(), "restarted")
                self.clock.advance(RESTART_GRACE_SECONDS)
                self.assertEqual(watchdog.check(), "rebooting")

    def test_kiosk_stopped_by_hand_after_restart_is_not_rebooted(self) -> None:
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.runner.kiosk = "inactive"
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "ok")
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "ok")
        self.assertEqual(self.runner.actions, [COMMANDS["kiosk_restart"]])

    def test_refused_reboot_is_not_stamped_and_retried(self) -> None:
        self.runner.codes[COMMANDS["reboot"]] = 1
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "restarted")
        self.assertFalse(self.state_path.exists())
        self.runner.codes.clear()
        self.clock.advance(RESTART_GRACE_SECONDS)
        self.assertEqual(watchdog.check(), "rebooting")
        self.assertTrue(self.state_path.exists())

    def test_refused_reboot_restores_the_previous_stamp(self) -> None:
        old = WALL_NOW - REBOOT_MIN_INTERVAL_SECONDS - 1
        self.state_path.write_text(json.dumps({"last_reboot": old}), encoding="utf-8")
        self.runner.codes[COMMANDS["reboot"]] = 1
        watchdog = self.make()
        self.clock.advance(STALE_AFTER_SECONDS)
        watchdog.check()
        self.clock.advance(RESTART_GRACE_SECONDS)
        watchdog.check()
        self.assertEqual(json.loads(self.state_path.read_text())["last_reboot"], old)

    def test_frame_during_restart_command_wins(self) -> None:
        watchdog = self.make()
        self.runner.during[COMMANDS["kiosk_restart"]] = watchdog.note_frame
        self.clock.advance(STALE_AFTER_SECONDS)
        self.assertEqual(watchdog.check(), "ok")
        self.assertEqual(watchdog.status()["phase"], "ok")

    def test_runner_never_raises(self) -> None:
        code, out = _run(("/nonexistent/systemctl", "is-active", "x"))
        self.assertNotEqual(code, 0)
        self.assertTrue(out)


class KioskWatchdogRoutesTest(unittest.TestCase):
    def test_only_framed_polls_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = make_blueprint_app(Path(tmp))
            clock = FakeClock(monotonic=500.0)
            watchdog = KioskWatchdog(
                state_path=Path(tmp) / "kiosk-watchdog.json",
                runner=FakeRunner(),
                monotonic=clock.monotonic,
            )
            app.extensions["smart_display"]["kiosk_watchdog"] = watchdog
            client = app.test_client()

            client.get("/api/state")
            self.assertIsNone(watchdog.frame_age_seconds())
            client.get("/api/state", headers={FRAME_HEADER: "1"})
            clock.advance(7)
            self.assertEqual(watchdog.frame_age_seconds(), 7.0)

            health = client.get("/health").get_json()
            self.assertEqual(health["kiosk"], {"phase": "ok", "frame_age_seconds": 7.0})

    def test_routes_work_without_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = make_blueprint_app(Path(tmp)).test_client()
            self.assertEqual(client.get("/api/state", headers={FRAME_HEADER: "1"}).status_code, 200)
            self.assertNotIn("kiosk", client.get("/health").get_json())


class SudoersMatchesCommandsTest(unittest.TestCase):
    def test_every_command_is_granted_verbatim(self) -> None:
        text = (REPO / "deploy" / "sudoers" / "smart-display").read_text(encoding="utf-8")
        rule = next(line for line in text.splitlines() if line.startswith("pi "))
        granted = {part.strip() for part in rule.split("NOPASSWD:", 1)[1].split(",")}
        expected = {" ".join(argv[2:]) for argv in COMMANDS.values()}
        self.assertEqual(granted, expected)
        for argv in COMMANDS.values():
            self.assertEqual(argv[:2], ("sudo", "-n"))


if __name__ == "__main__":
    unittest.main()
