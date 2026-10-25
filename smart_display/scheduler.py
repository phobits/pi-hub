"""Lightweight per-job thread scheduler with jitter and pause groups.

Plan B2/S3:
- ``ScheduledJob.startup_delay_seconds`` staggers boot fan-out so providers do
  not all hit their upstream APIs in the first second after the Pi comes up.
- ``ScheduledJob.jitter_seconds`` adds bounded randomness to the first tick
  and to every interval afterwards, which spreads refreshes further on long
  uptimes.
- ``ScheduledJob.pause_group`` lets the frontend ask the scheduler to skip
  one or more job groups (e.g. "spotify" while the screensaver is on). Jobs
  keep rescheduling normally so resuming is instant.
- ``ScheduledJob.retry_after`` (#19) may shorten the wait after a tick: it is
  asked after every run and returns seconds, or ``None`` for the interval.
  Used for the early retries of a failed first fetch.
- ``Scheduler.trigger(name)`` wakes the named job immediately without waiting
  for its next interval — used by :meth:`set_paused` to do a fresh fetch
  right after resume.

Stdlib only. Testable via an injectable ``rng`` callable; the test suite in
``tests/test_scheduler.py`` uses a deterministic stub to assert the wait
durations without spinning threads.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


logger = logging.getLogger(__name__)


# Screensaver-pause watchdog: if the frontend silently dies while the panel
# is showing the screensaver, we'd otherwise pause Spotify polling forever.
# The route refreshes this TTL on every state change, so a live frontend
# never hits the expiry. 10 minutes is well above any reasonable idle cycle
# and short enough that a crashed UI self-heals before anyone notices.
DEFAULT_PAUSE_TTL_SECONDS = 600.0


@dataclass
class ScheduledJob:
    name: str
    interval_seconds: float
    task: Callable[[], None]
    startup_delay_seconds: float = 0.0
    jitter_seconds: float = 0.0
    pause_group: str | None = None
    retry_after: Callable[[], float | None] | None = None


@dataclass
class _JobState:
    job: ScheduledJob
    trigger: threading.Event = field(default_factory=threading.Event)


class Scheduler:
    def __init__(
        self,
        *,
        rng: Callable[[float, float], float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._jobs: dict[str, _JobState] = {}
        # ``None`` means "paused indefinitely" (ops / manual); a float is
        # the monotonic-time expiry after which the pause self-clears.
        self._paused_groups: dict[str, float | None] = {}
        self._lock = threading.Lock()
        self._rng = rng or random.uniform
        self._monotonic = monotonic or time.monotonic

    def start(self, jobs: list[ScheduledJob]) -> None:
        for job in jobs:
            state = _JobState(job=job)
            with self._lock:
                self._jobs[job.name] = state
            thread = threading.Thread(
                target=self._run_job,
                args=(state,),
                name=f"job-{job.name}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            states = list(self._jobs.values())
        for state in states:
            state.trigger.set()
        for thread in self._threads:
            thread.join(timeout=1)

    # --- Pause / trigger API ------------------------------------------------

    def set_paused(
        self,
        group: str,
        paused: bool,
        *,
        ttl_seconds: float | None = None,
    ) -> None:
        """Pause (or resume) all jobs belonging to ``group``.

        Paused jobs keep rescheduling on their normal cadence but skip the
        task callable until the group is resumed. Resuming immediately
        triggers a fresh tick for every member of the group so the UI sees
        current data right away.

        ``ttl_seconds`` is the watchdog: a pause with a TTL auto-expires
        after that many seconds unless refreshed by another call. Without
        a TTL the pause is indefinite (manual / ops). The screensaver
        route uses a TTL so a crashed frontend cannot strand the Spotify
        polling forever.
        """
        with self._lock:
            if paused:
                expiry: float | None
                if ttl_seconds is None:
                    expiry = None
                else:
                    expiry = self._monotonic() + float(ttl_seconds)
                self._paused_groups[group] = expiry
                return
            self._paused_groups.pop(group, None)
            resume_names = [
                name
                for name, state in self._jobs.items()
                if state.job.pause_group == group
            ]
        for name in resume_names:
            self.trigger(name)

    def is_paused(self, group: str) -> bool:
        with self._lock:
            return self._group_is_paused_locked(group)

    def _group_is_paused_locked(self, group: str) -> bool:
        # Caller must hold ``self._lock``. Clears expired TTLs in-place so
        # a watchdog expiry is observed by the very next check without any
        # additional plumbing.
        if group not in self._paused_groups:
            return False
        expiry = self._paused_groups[group]
        if expiry is None:
            return True
        if self._monotonic() >= expiry:
            self._paused_groups.pop(group, None)
            logger.info("pause group %s auto-resumed after TTL", group)
            return False
        return True

    def trigger(self, name: str) -> None:
        """Wake the named job so it runs its next tick immediately."""
        with self._lock:
            state = self._jobs.get(name)
        if state is not None:
            state.trigger.set()

    # --- Wait-duration helpers (pure, testable) -----------------------------

    def compute_initial_wait(self, job: ScheduledJob) -> float:
        wait = float(job.startup_delay_seconds)
        if job.jitter_seconds > 0:
            wait += self._rng(0.0, float(job.jitter_seconds))
        return max(0.0, wait)

    def compute_next_wait(self, job: ScheduledJob) -> float:
        interval = float(job.interval_seconds)
        if job.retry_after is not None:
            try:
                retry = job.retry_after()
            except Exception:  # pragma: no cover — never let it stop the loop
                logger.exception("retry_after failed: %s", job.name)
                retry = None
            if retry is not None:
                interval = min(interval, float(retry))
        if job.jitter_seconds > 0:
            interval += self._rng(-float(job.jitter_seconds), float(job.jitter_seconds))
        return max(1.0, interval)

    # --- Job loop -----------------------------------------------------------

    def _run_job(self, state: _JobState) -> None:
        job = state.job
        initial = self.compute_initial_wait(job)
        if initial > 0:
            state.trigger.wait(initial)
        if self._stop_event.is_set():
            return
        state.trigger.clear()
        self._maybe_execute(job)
        while True:
            next_wait = self.compute_next_wait(job)
            state.trigger.wait(next_wait)
            if self._stop_event.is_set():
                return
            state.trigger.clear()
            self._maybe_execute(job)

    def _maybe_execute(self, job: ScheduledJob) -> None:
        if job.pause_group is not None:
            with self._lock:
                if self._group_is_paused_locked(job.pause_group):
                    return
        self._execute(job)

    def _execute(self, job: ScheduledJob) -> None:
        try:
            job.task()
        except Exception:  # pragma: no cover
            logger.exception("scheduled job failed: %s", job.name)
