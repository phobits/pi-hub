from __future__ import annotations

import threading
import time
import unittest

from smart_display.scheduler import (
    DEFAULT_PAUSE_TTL_SECONDS,
    ScheduledJob,
    Scheduler,
)


class ComputeWaitTest(unittest.TestCase):
    """Plan B2: wait-duration helpers are pure and deterministic so we can
    assert startup stagger and jitter clamps without spinning threads."""

    def test_initial_wait_equals_startup_delay_when_no_jitter(self) -> None:
        scheduler = Scheduler(rng=lambda a, b: 0.0)
        job = ScheduledJob(
            name="weather",
            interval_seconds=900,
            task=lambda: None,
            startup_delay_seconds=4.0,
            jitter_seconds=0.0,
        )
        self.assertEqual(scheduler.compute_initial_wait(job), 4.0)

    def test_initial_wait_adds_positive_jitter(self) -> None:
        # rng returns the upper bound, so we expect startup + jitter.
        scheduler = Scheduler(rng=lambda a, b: b)
        job = ScheduledJob(
            name="calendar",
            interval_seconds=300,
            task=lambda: None,
            startup_delay_seconds=2.0,
            jitter_seconds=1.5,
        )
        self.assertEqual(scheduler.compute_initial_wait(job), 3.5)

    def test_next_wait_applies_symmetric_jitter(self) -> None:
        # rng returns the lower bound → interval - jitter.
        scheduler = Scheduler(rng=lambda a, b: a)
        job = ScheduledJob(
            name="spotify",
            interval_seconds=30,
            task=lambda: None,
            jitter_seconds=5.0,
        )
        self.assertEqual(scheduler.compute_next_wait(job), 25.0)

    def test_next_wait_clamps_to_one_second(self) -> None:
        # Even if jitter would push us below 1s we never wait less.
        scheduler = Scheduler(rng=lambda a, b: a)
        job = ScheduledJob(
            name="fast",
            interval_seconds=2,
            task=lambda: None,
            jitter_seconds=5.0,
        )
        self.assertEqual(scheduler.compute_next_wait(job), 1.0)

    def test_retry_after_shortens_the_wait_only_while_it_asks(self) -> None:
        # #19: a failed first fetch retries early, then the interval resumes.
        scheduler = Scheduler(rng=lambda a, b: 0.0)
        answers = [30.0, None]
        job = ScheduledJob(
            name="weather",
            interval_seconds=900,
            task=lambda: None,
            retry_after=lambda: answers.pop(0),
        )
        self.assertEqual(scheduler.compute_next_wait(job), 30.0)
        self.assertEqual(scheduler.compute_next_wait(job), 900.0)

    def test_retry_after_never_lengthens_the_interval(self) -> None:
        scheduler = Scheduler(rng=lambda a, b: 0.0)
        job = ScheduledJob(
            name="spotify",
            interval_seconds=15,
            task=lambda: None,
            retry_after=lambda: 30.0,
        )
        self.assertEqual(scheduler.compute_next_wait(job), 15.0)

    def test_startup_delays_are_staggered_across_jobs(self) -> None:
        """With rng=0 the initial wait equals the declared startup delay."""
        scheduler = Scheduler(rng=lambda a, b: 0.0)
        jobs = [
            ScheduledJob("weather", 900, lambda: None, startup_delay_seconds=0.0, jitter_seconds=1.0),
            ScheduledJob("calendar", 300, lambda: None, startup_delay_seconds=2.0, jitter_seconds=1.0),
            ScheduledJob("spotify", 30, lambda: None, startup_delay_seconds=4.0, jitter_seconds=1.0),
            ScheduledJob("screensaver", 1800, lambda: None, startup_delay_seconds=6.0, jitter_seconds=1.0),
        ]
        waits = [scheduler.compute_initial_wait(job) for job in jobs]
        self.assertEqual(waits, [0.0, 2.0, 4.0, 6.0])


class PauseAndTriggerTest(unittest.TestCase):
    """Plan B2/B1: verify the pause group and trigger wiring against a real
    thread with a short interval. These are still fast (<200 ms each)."""

    def _make_counter_job(
        self,
        *,
        name: str,
        interval: float,
        pause_group: str | None = None,
    ):
        state = {"count": 0, "event": threading.Event()}

        def task() -> None:
            state["count"] += 1
            state["event"].set()

        job = ScheduledJob(
            name=name,
            interval_seconds=interval,
            task=task,
            startup_delay_seconds=0.0,
            jitter_seconds=0.0,
            pause_group=pause_group,
        )
        return job, state

    def test_paused_group_skips_task_but_reschedules(self) -> None:
        scheduler = Scheduler()
        job, state = self._make_counter_job(name="spotify", interval=0.05, pause_group="spotify")
        scheduler.set_paused("spotify", True)
        scheduler.start([job])
        try:
            # Let several ticks fire; none should execute the task.
            time.sleep(0.25)
            self.assertEqual(state["count"], 0)
            self.assertTrue(scheduler.is_paused("spotify"))
        finally:
            scheduler.stop()

    def test_resume_triggers_immediate_tick(self) -> None:
        scheduler = Scheduler()
        job, state = self._make_counter_job(
            name="spotify",
            interval=10.0,  # long interval — without trigger we'd never tick
            pause_group="spotify",
        )
        scheduler.set_paused("spotify", True)
        scheduler.start([job])
        try:
            time.sleep(0.05)
            # Still paused; the first tick fires but is skipped.
            self.assertEqual(state["count"], 0)
            scheduler.set_paused("spotify", False)
            self.assertTrue(state["event"].wait(timeout=1.0))
            self.assertGreaterEqual(state["count"], 1)
        finally:
            scheduler.stop()

    def test_trigger_wakes_named_job(self) -> None:
        scheduler = Scheduler()
        job, state = self._make_counter_job(name="weather", interval=10.0)
        scheduler.start([job])
        try:
            # First tick runs immediately (startup_delay=0).
            self.assertTrue(state["event"].wait(timeout=1.0))
            state["event"].clear()
            scheduler.trigger("weather")
            self.assertTrue(state["event"].wait(timeout=1.0))
            self.assertGreaterEqual(state["count"], 2)
        finally:
            scheduler.stop()

    def test_failing_task_does_not_kill_loop(self) -> None:
        scheduler = Scheduler()
        state = {"count": 0, "event": threading.Event()}

        def task() -> None:
            state["count"] += 1
            if state["count"] == 1:
                raise RuntimeError("boom")
            state["event"].set()

        # Long native interval — we rely on ``trigger`` to pump the second
        # tick so the test stays deterministic even under load.
        job = ScheduledJob(
            name="flaky",
            interval_seconds=10.0,
            task=task,
            startup_delay_seconds=0.0,
            jitter_seconds=0.0,
        )
        scheduler.start([job])
        try:
            # Wait for the first (failing) tick to land.
            deadline = time.monotonic() + 2.0
            while state["count"] < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(state["count"], 1)
            scheduler.trigger("flaky")
            self.assertTrue(state["event"].wait(timeout=2.0))
            self.assertGreaterEqual(state["count"], 2)
        finally:
            scheduler.stop()


class PauseTtlWatchdogTest(unittest.TestCase):
    """Plan B1 watchdog: the screensaver route pauses the Spotify group with
    a TTL so a crashed frontend cannot strand the polling forever. These
    tests inject a monotonic clock to exercise the expiry paths without
    wall-clock sleeps."""

    def _make_clock(self, start: float = 1000.0):
        now = {"value": start}

        def monotonic() -> float:
            return now["value"]

        def advance(delta: float) -> None:
            now["value"] += delta

        return monotonic, advance

    def test_pause_without_ttl_stays_indefinite(self) -> None:
        monotonic, advance = self._make_clock()
        scheduler = Scheduler(monotonic=monotonic)
        scheduler.set_paused("spotify", True)
        self.assertTrue(scheduler.is_paused("spotify"))
        advance(10_000.0)  # far beyond any TTL
        self.assertTrue(scheduler.is_paused("spotify"))

    def test_pause_with_ttl_auto_resumes_after_expiry(self) -> None:
        monotonic, advance = self._make_clock()
        scheduler = Scheduler(monotonic=monotonic)
        scheduler.set_paused("spotify", True, ttl_seconds=30.0)
        self.assertTrue(scheduler.is_paused("spotify"))
        advance(29.0)
        self.assertTrue(scheduler.is_paused("spotify"))
        advance(2.0)  # now 31s past the set call
        self.assertFalse(scheduler.is_paused("spotify"))

    def test_pause_with_ttl_refreshes_on_re_set(self) -> None:
        monotonic, advance = self._make_clock()
        scheduler = Scheduler(monotonic=monotonic)
        scheduler.set_paused("spotify", True, ttl_seconds=30.0)
        advance(25.0)
        # Healthy frontend heartbeat — refresh the pause.
        scheduler.set_paused("spotify", True, ttl_seconds=30.0)
        advance(20.0)  # 45s after first set, but only 20s after refresh
        self.assertTrue(scheduler.is_paused("spotify"))
        advance(15.0)  # 35s after refresh
        self.assertFalse(scheduler.is_paused("spotify"))

    def test_paused_job_auto_resumes_mid_loop_when_ttl_expires(self) -> None:
        """End-to-end: a running job whose group was paused with a TTL must
        start executing again once the TTL lapses, without any explicit
        resume call. Proves the watchdog path is wired into ``_maybe_execute``
        and not just ``is_paused``."""
        monotonic, advance = self._make_clock()
        scheduler = Scheduler(monotonic=monotonic)
        state = {"count": 0, "event": threading.Event()}

        def task() -> None:
            state["count"] += 1
            state["event"].set()

        job = ScheduledJob(
            name="spotify",
            interval_seconds=0.05,
            task=task,
            startup_delay_seconds=0.0,
            jitter_seconds=0.0,
            pause_group="spotify",
        )
        scheduler.set_paused("spotify", True, ttl_seconds=10.0)
        scheduler.start([job])
        try:
            time.sleep(0.15)
            self.assertEqual(state["count"], 0)
            # Fast-forward the injected clock past the TTL; the next tick
            # observes the expiry and runs the task.
            advance(11.0)
            self.assertTrue(state["event"].wait(timeout=1.0))
            self.assertGreaterEqual(state["count"], 1)
        finally:
            scheduler.stop()

    def test_default_pause_ttl_is_generous_but_finite(self) -> None:
        # Guardrail: the constant shared between scheduler and routes must
        # be large enough to survive one full idle cycle on a sleepy panel
        # but short enough that a stranded pause self-heals in minutes.
        self.assertGreaterEqual(DEFAULT_PAUSE_TTL_SECONDS, 300.0)
        self.assertLessEqual(DEFAULT_PAUSE_TTL_SECONDS, 1800.0)


if __name__ == "__main__":
    unittest.main()
