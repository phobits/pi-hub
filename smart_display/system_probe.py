"""Cheap host facts the UI needs (Ticket #1).

The Pi has no RTC: after a power cut it boots on the ``fake-hwclock`` time and
shows it with full confidence until NTP steps the clock. ``systemd-timesyncd``
creates ``/run/systemd/timesync/synchronized`` once it has synchronised, and
that file is all we read — a stat per ``/api/state`` poll, no subprocess.

Anything that is *not* a timesyncd host (macOS dev box, a Pi on chrony) has no
such directory. We treat "unknown" as synchronised so the hint never nags on a
machine that cannot answer the question.
"""
from __future__ import annotations

from pathlib import Path


SYSTEMD_RUN_DIR = Path("/run/systemd")


def time_is_synchronized(run_dir: Path | str = SYSTEMD_RUN_DIR) -> bool:
    base = Path(run_dir)
    timesync_dir = base / "timesync"
    if not timesync_dir.is_dir():
        # No timesyncd state at all: non-systemd host or a different NTP
        # client. Unknown → do not claim the clock is wrong.
        return True
    return (timesync_dir / "synchronized").exists()
