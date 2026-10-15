"""Countdown timers (Ticket #3). Stdlib only, state lives in the StateStore.

Status is derived from the clock, never stored:

    running  → now < ends_at
    ringing  → ends_at <= now < ends_at + RING_TIMEOUT_SECONDS and not dismissed
    done     → dismissed, or ringing for longer than RING_TIMEOUT_SECONDS

The panel has no speaker, so "ringing" is purely visual — which is also why it
times out: nobody home must not mean a pulsing alarm for the rest of the day.
Done timers are pruned on the next mutation; reads never write.
"""
from __future__ import annotations

import secrets
import time
from typing import Any, Callable

from smart_display.models import TimerState
from smart_display.state_store import StateStore


MAX_TIMERS = 3
MIN_DURATION_SECONDS = 10
MAX_DURATION_SECONDS = 24 * 60 * 60
MAX_LABEL_LENGTH = 40
RING_TIMEOUT_SECONDS = 600


def timer_status(timer: TimerState, now: float) -> str:
    if timer.dismissed:
        return "done"
    if now < timer.ends_at:
        return "running"
    if now < timer.ends_at + RING_TIMEOUT_SECONDS:
        return "ringing"
    return "done"


def describe(timer: TimerState, now: float) -> dict[str, Any]:
    payload = timer.to_dict()
    payload["status"] = timer_status(timer, now)
    payload["remaining_seconds"] = max(0, int(round(timer.ends_at - now)))
    return payload


class TimerService:
    def __init__(
        self,
        state_store: StateStore,
        *,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = state_store
        self._clock = clock or time.time
        self._id_factory = id_factory or (lambda: secrets.token_hex(4))

    # ---- reads ---------------------------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        now = self._clock()
        return [
            describe(timer, now)
            for timer in self._store.get_timers()
            if timer_status(timer, now) != "done"
        ]

    # ---- mutations -----------------------------------------------------------

    def create(self, duration_seconds: Any, label: Any = "") -> dict[str, Any]:
        try:
            duration = int(duration_seconds)
        except (TypeError, ValueError):
            return self._fail("Dauer fehlt oder ist ungültig.")
        if not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
            return self._fail("Dauer muss zwischen 10 Sekunden und 24 Stunden liegen.")
        clean_label = "" if label is None else str(label).strip()[:MAX_LABEL_LENGTH]

        now = self._clock()
        live = self._live_timers(now)
        if len(live) >= MAX_TIMERS:
            return self._fail(f"Höchstens {MAX_TIMERS} Timer gleichzeitig.")
        timer = TimerState(
            id=self._unique_id(live),
            label=clean_label,
            duration_seconds=duration,
            ends_at=now + duration,
            created_at=now,
        )
        live.append(timer)
        self._store.set_timers(live)
        return self._ok(now, message="Timer gestartet.")

    def cancel(self, timer_id: str) -> dict[str, Any]:
        now = self._clock()
        live = self._live_timers(now)
        remaining = [timer for timer in live if timer.id != timer_id]
        if len(remaining) == len(live):
            return self._fail("Timer nicht gefunden.")
        self._store.set_timers(remaining)
        return self._ok(now, message="Timer abgebrochen.")

    def dismiss(self, timer_id: str) -> dict[str, Any]:
        now = self._clock()
        live = self._live_timers(now)
        found = False
        for timer in live:
            if timer.id == timer_id:
                timer.dismissed = True
                found = True
        if not found:
            return self._fail("Timer nicht gefunden.")
        self._store.set_timers([t for t in live if not t.dismissed])
        return self._ok(now, message="Timer beendet.")

    def extend(self, timer_id: str, seconds: Any) -> dict[str, Any]:
        try:
            extra = int(seconds)
        except (TypeError, ValueError):
            return self._fail("Verlängerung fehlt oder ist ungültig.")
        if not 1 <= extra <= MAX_DURATION_SECONDS:
            return self._fail("Verlängerung ist ungültig.")
        now = self._clock()
        live = self._live_timers(now)
        for timer in live:
            if timer.id != timer_id:
                continue
            # A ringing timer restarts from *now*; a running one just grows.
            base = timer.ends_at if timer.ends_at > now else now
            new_end = base + extra
            if new_end - now > MAX_DURATION_SECONDS:
                return self._fail("Timer darf höchstens 24 Stunden laufen.")
            timer.ends_at = new_end
            timer.duration_seconds = int(round(new_end - timer.created_at))
            self._store.set_timers(live)
            return self._ok(now, message="Timer verlängert.")
        return self._fail("Timer nicht gefunden.")

    # ---- internals -----------------------------------------------------------

    def _live_timers(self, now: float) -> list[TimerState]:
        return [
            timer
            for timer in self._store.get_timers()
            if timer_status(timer, now) != "done"
        ]

    def _unique_id(self, existing: list[TimerState]) -> str:
        taken = {timer.id for timer in existing}
        for _ in range(16):
            candidate = self._id_factory()
            if candidate not in taken:
                return candidate
        raise RuntimeError("could not allocate a unique timer id")

    def _ok(self, now: float, *, message: str) -> dict[str, Any]:
        return {"ok": True, "message": message, "timers": self.list()}

    @staticmethod
    def _fail(message: str) -> dict[str, Any]:
        return {"ok": False, "message": message}
