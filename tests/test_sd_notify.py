from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from smart_display import sd_notify


class NotifyTest(unittest.TestCase):
    def test_noop_without_notify_socket(self) -> None:
        self.assertFalse(sd_notify.notify("READY=1", environ={}))

    def test_sends_datagram_to_notify_socket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "notify.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
                server.bind(path)
                server.settimeout(2)
                self.assertTrue(sd_notify.notify("READY=1", environ={"NOTIFY_SOCKET": path}))
                self.assertEqual(server.recv(64), b"READY=1")

    def test_unreachable_socket_is_logged_not_raised(self) -> None:
        self.assertFalse(
            sd_notify.notify("READY=1", environ={"NOTIFY_SOCKET": "/nonexistent/sock"})
        )


class WatchdogIntervalTest(unittest.TestCase):
    def test_half_of_watchdog_usec(self) -> None:
        self.assertEqual(sd_notify.watchdog_interval_seconds({"WATCHDOG_USEC": "90000000"}), 30.0)

    def test_unset_or_invalid(self) -> None:
        self.assertIsNone(sd_notify.watchdog_interval_seconds({}))
        self.assertIsNone(sd_notify.watchdog_interval_seconds({"WATCHDOG_USEC": "x"}))
        self.assertIsNone(sd_notify.watchdog_interval_seconds({"WATCHDOG_USEC": "0"}))

    def test_other_pid_is_ignored(self) -> None:
        env = {"WATCHDOG_USEC": "90000000", "WATCHDOG_PID": str(os.getpid() + 1)}
        self.assertIsNone(sd_notify.watchdog_interval_seconds(env))
        env["WATCHDOG_PID"] = str(os.getpid())
        self.assertEqual(sd_notify.watchdog_interval_seconds(env), 30.0)


class WatchdogPingerTest(unittest.TestCase):
    def test_pings_only_when_health_probe_succeeds(self) -> None:
        sent: list[str] = []
        pinger = sd_notify.WatchdogPinger(
            interval=10,
            host="127.0.0.1",
            port=8080,
            notifier=lambda msg: sent.append(msg) or True,
            prober=lambda host, port, timeout: True,
        )
        self.assertTrue(pinger.tick())
        self.assertEqual(sent, ["WATCHDOG=1"])

        silent = sd_notify.WatchdogPinger(
            interval=10,
            host="127.0.0.1",
            port=8080,
            notifier=lambda msg: sent.append(msg) or True,
            prober=lambda host, port, timeout: False,
        )
        self.assertFalse(silent.tick())
        self.assertEqual(sent, ["WATCHDOG=1"])

    def test_real_probe_fails_fast_on_closed_port(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        self.assertFalse(sd_notify.probe_health("127.0.0.1", free_port, 0.5))


if __name__ == "__main__":
    unittest.main()
