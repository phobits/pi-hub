"""systemd notify + watchdog integration, stdlib only (Ticket #1).

``smart-display.service`` runs as ``Type=notify`` with ``WatchdogSec``. Two
duties follow from that:

- ``READY=1`` once Waitress has bound its socket, so systemd (and the kiosk
  unit's ordering) knows the backend is really up.
- ``WATCHDOG=1`` on a timer — but only after a real loopback ``GET /health``
  succeeded. A heartbeat that merely proves "the Python thread is alive" would
  keep a wedged Waitress running forever; probing the HTTP path is what turns
  the watchdog into an actual liveness check.

Outside systemd (``NOTIFY_SOCKET`` unset) every call is a no-op, so the local
demo server and the tests behave exactly as before.
"""
from __future__ import annotations

import http.client
import logging
import os
import socket
import threading
from typing import Callable


logger = logging.getLogger(__name__)


def notify(message: str, *, environ: dict[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    address = env.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        # Abstract namespace socket.
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)
        return False


def watchdog_interval_seconds(environ: dict[str, str] | None = None) -> float | None:
    """Half of ``WATCHDOG_USEC`` (systemd's recommended ping cadence), or
    ``None`` when no watchdog is configured for this process."""
    env = environ if environ is not None else os.environ
    raw = env.get("WATCHDOG_USEC")
    if not raw:
        return None
    pid = env.get("WATCHDOG_PID")
    if pid and pid.isdigit() and int(pid) != os.getpid():
        return None
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    return max(1.0, usec / 1_000_000 / 2)


def probe_health(host: str, port: int, timeout: float) -> bool:
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read(512)
            return response.status == 200
        finally:
            conn.close()
    except OSError:
        return False


class WatchdogPinger(threading.Thread):
    """Daemon thread: every ``interval`` seconds probe ``/health`` and, on
    success, send ``WATCHDOG=1``. On failure it logs and stays silent so
    systemd restarts the unit once ``WatchdogSec`` elapses."""

    def __init__(
        self,
        *,
        interval: float,
        host: str,
        port: int,
        notifier: Callable[[str], bool] = notify,
        prober: Callable[[str, int, float], bool] = probe_health,
    ) -> None:
        super().__init__(name="sd-watchdog", daemon=True)
        self.interval = float(interval)
        self.host = host
        self.port = port
        self._notifier = notifier
        self._prober = prober
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover — exercised via tick()
        while not self._stop_event.wait(self.interval):
            self.tick()

    def tick(self) -> bool:
        timeout = max(0.5, min(5.0, self.interval / 2))
        if self._prober(self.host, self.port, timeout):
            self._notifier("WATCHDOG=1")
            return True
        logger.warning(
            "watchdog probe of /health failed; withholding WATCHDOG=1 so systemd can restart us"
        )
        return False
