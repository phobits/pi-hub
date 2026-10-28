"""Frozen-panel watchdog (#23).

The systemd watchdog only proves the backend answers. On 2026-10-28 the vc4
GPU hung, Cog stopped drawing, and the panel showed 14:03 for 90 minutes
while every unit stayed ``active``. Restarting the kiosk did not help either:
one Cog thread sat in ``D`` state inside ``drm_release`` and survived SIGKILL,
so only a reboot healed it.

Signal: ``app.js`` gates every scheduled state poll behind a
``requestAnimationFrame`` and marks it with ``X-Kiosk-Frame: 1`` only when the
frame actually arrived. While the compositor hangs, no frame arrives, and the
fallback poll that keeps data flowing goes out unmarked. A marked poll is
therefore evidence that the page still renders, not merely that its JS runs.

Escalation, one step per stale episode:

1. no marked poll for ``STALE_AFTER_SECONDS`` and the kiosk unit is
   ``active`` → restart the kiosk (``--no-block``: the stop can hang for good)
2. still none ``RESTART_GRACE_SECONDS`` later → reboot, at most once per
   ``REBOOT_MIN_INTERVAL_SECONDS``; the last watchdog reboot is kept in
   ``data/kiosk-watchdog.json`` because the monotonic clock does not survive it

A kiosk that is ``inactive`` was stopped on purpose (debugging,
``test-panel-blank.sh``) and is left alone, before either step. Every other
state counts as broken — ``activating``/``failed`` is a crash loop
(``Restart=always``), ``deactivating`` the stop that hung on 2026-10-28. Runs only where
``app.allow_shutdown`` is set — the same gate and the same ``sudo -n`` argv
discipline as the power buttons (``power.py``).
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from smart_display.power import COMMANDS, SYSTEMCTL


logger = logging.getLogger(__name__)

# The screensaver polls every 5 min, so three of its polls must be missable.
STALE_AFTER_SECONDS = 15 * 60
RESTART_GRACE_SECONDS = 10 * 60
REBOOT_MIN_INTERVAL_SECONDS = 6 * 60 * 60
CHECK_INTERVAL_SECONDS = 60.0

KIOSK_UNIT = "smart-display-kiosk.service"
IS_ACTIVE_ARGV: tuple[str, ...] = (SYSTEMCTL, "is-active", KIOSK_UNIT)
FRAME_HEADER = "X-Kiosk-Frame"


def _run(argv: Sequence[str]) -> tuple[int, str]:
    # Never raises: a hung systemctl must not stall the escalation (#23).
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    out = (completed.stdout or completed.stderr).decode("utf-8", "replace").strip()
    return completed.returncode, out


class KioskWatchdog:
    def __init__(
        self,
        *,
        state_path: Path,
        runner: Callable[[Sequence[str]], tuple[int, str]] = _run,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._state_path = state_path
        self._runner = runner
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self._started_at = monotonic()
        self._last_frame_at: float | None = None
        # ok → restarted → rebooting | held (reboot refused by the interval)
        self._phase = "ok"
        self._restarted_at = 0.0
        self._idle_logged = False

    def note_frame(self) -> None:
        with self._lock:
            self._last_frame_at = self._monotonic()
            if self._phase != "ok":
                logger.warning("kiosk watchdog: panel renders again (was %s)", self._phase)
            self._phase = "ok"
            self._idle_logged = False

    def frame_age_seconds(self) -> float | None:
        with self._lock:
            return self._frame_age_locked()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"phase": self._phase, "frame_age_seconds": self._frame_age_locked()}

    def _frame_age_locked(self) -> float | None:
        if self._last_frame_at is None:
            return None
        return round(self._monotonic() - self._last_frame_at, 1)

    def check(self) -> str:
        """One watchdog tick; returns the phase afterwards."""
        with self._lock:
            now = self._monotonic()
            frame = self._last_frame_at
            age = now - (frame if frame is not None else self._started_at)
            phase = self._phase
            restarted_at = self._restarted_at
        if phase == "ok" and age < STALE_AFTER_SECONDS:
            return phase
        if phase == "restarted" and now - restarted_at < RESTART_GRACE_SECONDS:
            return phase
        if phase not in ("ok", "restarted", "held"):
            return phase
        if self._stopped_on_purpose(age):
            return self._transition(frame, "ok")
        if phase == "ok":
            return self._restart_kiosk(age, frame)
        return self._reboot(age, frame, phase)

    def _stopped_on_purpose(self, age: float) -> bool:
        _code, out = self._runner(IS_ACTIVE_ARGV)
        if out != "inactive":
            return False
        # A frame arriving later re-arms the log line.
        if not self._idle_logged:
            logger.info("kiosk watchdog: no frame for %.0f s, kiosk is inactive — not intervening", age)
            self._idle_logged = True
        return True

    def _transition(self, frame: float | None, phase: str) -> str:
        """Move to ``phase`` unless a frame arrived since ``check()`` looked —
        the commands take seconds, and note_frame() must win that race."""
        with self._lock:
            if self._last_frame_at != frame:
                return self._phase
            self._phase = phase
            if phase == "restarted":
                self._restarted_at = self._monotonic()
            return phase

    def _restart_kiosk(self, age: float, frame: float | None) -> str:
        logger.warning("kiosk watchdog: no frame for %.0f s, restarting the kiosk", age)
        code, out = self._runner(COMMANDS["kiosk_restart"])
        if code != 0:
            logger.warning("kiosk watchdog: kiosk restart refused (%s): %s", code, out)
        return self._transition(frame, "restarted")

    def _reboot(self, age: float, frame: float | None, phase: str) -> str:
        last = self._last_reboot()
        now = self._wall()
        # A clock behind the stored stamp (no RTC, before NTP) is negative
        # here and therefore counts as recent.
        if last is not None and now - last < REBOOT_MIN_INTERVAL_SECONDS:
            if phase != "held":
                logger.warning(
                    "kiosk watchdog: no frame for %.0f s after a kiosk restart; "
                    "reboot held, last watchdog reboot was %.0f s ago",
                    age,
                    now - last,
                )
            return self._transition(frame, "held")
        logger.warning("kiosk watchdog: no frame for %.0f s after a kiosk restart, rebooting", age)
        # Stamped first: the shutdown that follows a successful call could cut
        # a later write short, and the stamp is what stops a reboot loop.
        self._store_reboot(now)
        code, out = self._runner(COMMANDS["reboot"])
        if code != 0:
            # A refused reboot must not hold the next one back; it is retried
            # after another grace period.
            logger.warning("kiosk watchdog: reboot refused (%s): %s", code, out)
            self._store_reboot(last)
            return self._transition(frame, "restarted")
        return self._transition(frame, "rebooting")

    def _last_reboot(self) -> float | None:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))["last_reboot"]
            return float(value)
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _store_reboot(self, at: float | None) -> None:
        try:
            if at is None:
                self._state_path.unlink(missing_ok=True)
                return
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_reboot": at}), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            logger.warning("kiosk watchdog: could not store reboot stamp: %s", exc)


class KioskWatchdogThread(threading.Thread):
    def __init__(self, watchdog: KioskWatchdog, interval: float = CHECK_INTERVAL_SECONDS) -> None:
        super().__init__(name="kiosk-watchdog", daemon=True)
        self.watchdog = watchdog
        self.interval = float(interval)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # pragma: no cover — exercised via KioskWatchdog.check()
        while not self._stop_event.wait(self.interval):
            try:
                self.watchdog.check()
            except Exception:  # noqa: BLE001 — a watchdog bug must not end the loop
                logger.exception("kiosk watchdog check failed")
