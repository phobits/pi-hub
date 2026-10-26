"""JSON fetch with a polite failure path for the weather providers (#22).

``HttpClient`` already rebuilds a dropped connection and retries once; that
is the only immediate retry. What it cannot do is notice that the *server*
asks us to slow down. ``fetch_json`` turns every outcome into a ``FetchError``
with two facts the provider needs — can a later retry help (``transient``),
and how long must it at least wait (``retry_after``) — and ``Backoff`` keeps
a provider from calling again before that.

Backoff only applies to answers that come from the server (429, 5xx).
Network failures (no DNS, timeout) cost the upstream nothing and keep the
normal cadence, including the boot grace's early retries (#19).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from smart_display.http_client import HttpClient, HttpError

# A Retry-After beyond this is treated as a misconfigured server, not an order.
MAX_RETRY_AFTER_SECONDS = 6 * 3600.0
# Consecutive 5xx answers stretch the wait 1×, 2×, 4× the interval, no further.
MAX_BACKOFF_FACTOR = 4


class FetchError(Exception):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = True,
        retry_after: float | None = None,
        status: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.retry_after = retry_after
        self.status = status
        # The start of the error body, for callers that tell one 4xx from
        # another ("outside of dataset bounds" vs. a bad parameter).
        self.detail = detail


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        seconds = (moment - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds != seconds:  # NaN
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))


def fetch_json(
    http: HttpClient,
    url: str,
    *,
    timeout: float,
    label: str,
    permanent_statuses: frozenset[int] = frozenset({400, 401, 403, 404, 422}),
    max_bytes: int | None = None,
) -> Any:
    """GET ``url`` and decode JSON, or raise ``FetchError``. ``label`` names
    the source in German for the error message ("GeoSphere C-LAEF")."""
    try:
        response = http.get(url, timeout=timeout, max_bytes=max_bytes)
    except (HttpError, OSError) as exc:
        raise FetchError(f"{label} nicht erreichbar: {exc}") from exc
    if response.status == 429:
        raise FetchError(
            f"{label}: zu viele Anfragen (429)",
            retry_after=parse_retry_after(response.headers.get("retry-after")),
            status=429,
        )
    if response.status >= 500:
        raise FetchError(
            f"{label} antwortete mit {response.status}",
            retry_after=parse_retry_after(response.headers.get("retry-after")),
            status=response.status,
        )
    if not response.ok:
        raise FetchError(
            f"{label} antwortete mit {response.status}",
            transient=response.status not in permanent_statuses,
            status=response.status,
            detail=response.body[:300].decode("utf-8", "replace"),
        )
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise FetchError(f"{label}: ungültiges JSON ({exc})") from exc
    if payload is None:
        raise FetchError(f"{label}: leere Antwort")
    return payload


class Backoff:
    """Skips refreshes while the server asked for a pause.

    ``ready()`` is checked at the top of ``refresh()``; a skipped tick does
    not touch the state (the section keeps its status and its age).
    """

    def __init__(self, interval_seconds: float, *, monotonic: Callable[[], float] | None = None) -> None:
        self.interval = float(interval_seconds)
        self._monotonic = monotonic or time.monotonic
        self._server_failures = 0
        self._blocked_until = float("-inf")

    def ready(self) -> bool:
        return self._monotonic() >= self._blocked_until

    @property
    def blocked_for(self) -> float:
        return max(0.0, self._blocked_until - self._monotonic())

    def success(self) -> None:
        self._server_failures = 0
        self._blocked_until = float("-inf")

    def failure(self, error: FetchError) -> None:
        if error.status is None or not (error.status == 429 or error.status >= 500):
            return  # network-level or permanent: the normal cadence applies
        self._server_failures += 1
        factor = min(2 ** (self._server_failures - 1), MAX_BACKOFF_FACTOR)
        # The first failure keeps the normal cadence; from the second on, the
        # ticks before ``factor`` intervals are skipped.
        wait = factor * self.interval if factor > 1 else 0.0
        if error.retry_after is not None:
            wait = max(wait, error.retry_after)
        # Leave a second of slack so the tick that lands right on the
        # boundary is let through rather than skipped for a whole interval.
        self._blocked_until = self._monotonic() + max(0.0, wait - 1.0)
