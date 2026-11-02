"""Status of the pi5 (DNS, backups, containers) for the pi5 screen (#24).

The pi5 serves ``pistatus --json`` on a socket-activated port (repo
``pi5-config``); every connection runs the script once (~0.1 s) and nothing
stays resident. This provider is the only reader and it reads **on demand**:
the frontend polls ``/api/pi5`` only while the pi5 screen is visible, like the
Spotify queue. No scheduler job, no ``DashboardState`` section, no
``last_good.json`` write — live temperatures are worthless after a reboot, and
a background job would break the ``/api/state`` ETag every minute.

The pi5 computes the traffic lights itself (thresholds live in ``pistatus``
only); this side validates and passes them through. Anything unexpected
becomes ``unknown``/``None``, never a made-up ``ok`` or ``0``.
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from smart_display.config import AppConfig
from smart_display.http_client import HttpClient
from smart_display.providers._fetch import FetchError, fetch_json

# Several requests within this window share one fetch (and one failure).
CACHE_TTL_SECONDS = 10.0
# After a failed fetch the last good data is shown with its age for this long;
# after that the screen says "nicht erreichbar" instead of hour-old numbers.
STALE_MAX_AGE_SECONDS = 600.0
# The answer is 1–2 KB (a few containers, 8 Pi-hole clients) and at most ~6 KB
# with every cap below full; anything near this cap is not pistatus.
MAX_BYTES = 16_000
# A Docker host rarely has more; the screen shows fewer anyway.
MAX_CONTAINERS = 30
# pistatus sends the top 8 Pi-hole clients and the screen shows 6; the cap
# only bounds what a future pistatus could send.
MAX_PIHOLE_CLIENTS = 10

LEVELS = frozenset({"ok", "warn", "bad"})
RUN_STATES = frozenset({"ok", "stale", "failed", "unknown", "running", "never"})
HEALTH = frozenset({"healthy", "unhealthy", "starting", "none"})
BLOCKING = frozenset({"enabled", "disabled", "failed", "unknown"})
CONTAINER_STATES = frozenset(
    {"created", "restarting", "running", "removing", "paused", "exited", "dead"}
)

DEMO_STATUS: dict[str, Any] = {
    "host": "pi5",
    "now": None,  # filled in per call, so the demo uptime looks alive
    "boot": None,
    "temp_c": 47.2,
    "temp_level": "ok",
    "fan_rpm": 0,
    "throttled": "0x0",
    "throttled_level": "ok",
    "ram": {"used": 636_895_232, "total": 17_006_116_864, "pct": 4, "level": "ok"},
    "disk": {"used": 6_347_763_712, "total": 62_256_185_344, "pct": 11, "level": "ok"},
    "backup": {"state": "ok", "epoch": None, "result": None},
    "update": {"state": "unknown", "epoch": None, "result": None},
    "travelog_backup": None,
    "pihole": {
        "blocking": "enabled",
        "total": 71_509,
        "blocked": 17_695,
        "percent": 24.7,
        "clients": 13,
        "top": [
            {"name": "Forte.fritz.box", "ip": "192.168.178.13", "count": 27_961},
            {"name": "Mac.fritz.box", "ip": "192.168.178.38", "count": 18_315},
            {"name": "mPhone15.fritz.box", "ip": "192.168.178.10", "count": 12_274},
            {"name": "eleven.fritz.box", "ip": "192.168.178.11", "count": 5_242},
            {"name": "iPhone-Kathi.fritz.box", "ip": "192.168.178.79", "count": 3_589},
            {"name": "ebe63ef5-f980-67f3-0866-b6e2899fa7a2.fritz.box", "ip": "192.168.178.26", "count": 1_997},
            {"name": "pihub.fritz.box", "ip": "192.168.178.22", "count": 1_328},
        ],
    },
    "containers": [
        {"name": "pihole", "state": "running", "health": "healthy"},
        {"name": "travelog-backup-1", "state": "running", "health": "healthy"},
        {"name": "travelog-travelog-1", "state": "running", "health": "healthy"},
        {"name": "spotipi", "state": "running", "health": "healthy"},
    ],
}


class Pi5StatusProvider:
    def __init__(
        self,
        config: AppConfig,
        *,
        http_client: HttpClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._http = http_client or HttpClient()
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        # Last answer of any kind, for the shared-fetch window.
        self._last_result: dict[str, Any] | None = None
        self._last_result_at = float("-inf")
        # Last good data, for the stale fallback.
        self._good: dict[str, Any] | None = None
        self._good_at = float("-inf")
        self._good_stamp: str | None = None

    @property
    def url(self) -> str:
        return self._config.pi5_status.url

    @property
    def enabled(self) -> bool:
        """Whether the pi5 screen exists at all."""
        return bool(self.url) or self._config.app.demo_mode

    def status(self) -> dict[str, Any]:
        if not self.url:
            if self._config.app.demo_mode:
                return self._result("ok", _demo_status(self._utcnow()), self._stamp())
            return self._result("disabled", None, None)

        with self._lock:
            now = self._monotonic()
            if self._last_result is not None and now - self._last_result_at < CACHE_TTL_SECONDS:
                return self._last_result
            result = self._fetch(now)
            self._last_result = result
            self._last_result_at = now
            return result

    def _fetch(self, now: float) -> dict[str, Any]:
        try:
            payload = fetch_json(
                self._http,
                self.url,
                timeout=float(self._config.pi5_status.timeout_seconds),
                label="pi5",
                max_bytes=MAX_BYTES,
            )
            data = normalize_status(payload)
            if data is None:
                raise FetchError("pi5: unerwartete Antwort")
        except (FetchError, ValueError) as exc:
            # ValueError: a URL the HTTP client cannot parse (bad port) — a
            # configuration error, but still this screen's problem only.
            if self._good is not None and now - self._good_at <= STALE_MAX_AGE_SECONDS:
                return self._result("stale", self._good, self._good_stamp, str(exc))
            return self._result("error", None, self._good_stamp, str(exc))
        self._good = data
        self._good_at = now
        self._good_stamp = self._stamp()
        return self._result("ok", data, self._good_stamp)

    def _stamp(self) -> str:
        return self._utcnow().isoformat(timespec="seconds")

    @staticmethod
    def _result(
        status: str, data: dict[str, Any] | None, updated_at: str | None, message: str = ""
    ) -> dict[str, Any]:
        return {"status": status, "updated_at": updated_at, "message": message, "data": data}


def normalize_status(payload: Any) -> dict[str, Any] | None:
    """Validate a ``pistatus --json`` answer. ``None`` if it is not one."""
    if not isinstance(payload, dict) or "temp_level" not in payload:
        return None
    containers_raw = payload.get("containers")
    if isinstance(containers_raw, list):
        containers: list[dict[str, Any]] | None = [
            _container(item) for item in containers_raw[:MAX_CONTAINERS] if isinstance(item, dict)
        ]
    else:
        containers = None  # Docker did not answer — not the same as "no containers"
    return {
        "host": _text(payload.get("host"), 32) or "pi5",
        "now": _int(payload.get("now")),
        "boot": _int(payload.get("boot")),
        "temp_c": _number(payload.get("temp_c")),
        "temp_level": _level(payload.get("temp_level")),
        "fan_rpm": _int(payload.get("fan_rpm")),
        "throttled": _text(payload.get("throttled"), 16),
        "throttled_level": _level(payload.get("throttled_level")),
        "ram": _usage(payload.get("ram")),
        "disk": _usage(payload.get("disk")),
        "backup": _run(payload.get("backup")),
        "update": _run(payload.get("update")),
        # #29: newest travelog backup (file mtime); None = none found or an old pistatus
        "travelog_backup": _int(payload.get("travelog_backup")),
        "pihole": _pihole(payload.get("pihole")),
        "containers": containers,
    }


def _demo_status(now: datetime) -> dict[str, Any]:
    epoch = int(now.timestamp())
    data = {**DEMO_STATUS, "now": epoch, "boot": epoch - 19 * 60}
    data["backup"] = {"state": "ok", "epoch": epoch - 3 * 3600, "result": None}
    data["update"] = {"state": "unknown", "epoch": epoch - 6 * 86400, "result": None}
    data["travelog_backup"] = epoch - 4 * 3600
    return data


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] or None


def _level(value: Any) -> str:
    return value if value in LEVELS else "unknown"


def _usage(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    used, total = _int(value.get("used")), _int(value.get("total"))
    pct = _int(value.get("pct"))
    return {
        "used": used,
        "total": total if total else None,
        "pct": pct if pct is not None and pct <= 100 else None,
        "level": _level(value.get("level")),
    }


def _run(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    state = value.get("state")
    return {
        "state": state if state in RUN_STATES else "unknown",
        "epoch": _int(value.get("epoch")),
        "result": _text(value.get("result"), 32),
    }


def _pihole(value: Any) -> dict[str, Any] | None:
    """Pi-hole's last 24 h. ``None`` = Pi-hole did not answer (or an old pistatus)."""
    if not isinstance(value, dict):
        return None
    blocking = value.get("blocking")
    percent = _number(value.get("percent"))
    clients = value.get("top")
    top = []
    if isinstance(clients, list):
        for item in clients[:MAX_PIHOLE_CLIENTS]:
            if not isinstance(item, dict):
                continue
            name, ip = _text(item.get("name"), 64), _text(item.get("ip"), 45)
            if name or ip:
                top.append({"name": name, "ip": ip, "count": _int(item.get("count"))})
    return {
        "blocking": blocking if blocking in BLOCKING else "unknown",
        "total": _int(value.get("total")),
        "blocked": _int(value.get("blocked")),
        "percent": percent if percent is not None and 0 <= percent <= 100 else None,
        "clients": _int(value.get("clients")),
        "top": top,
    }


def _container(value: dict[str, Any]) -> dict[str, Any]:
    state = value.get("state")
    health = value.get("health")
    return {
        "name": _text(value.get("name"), 64) or "?",
        "state": state if state in CONTAINER_STATES else "unknown",
        "health": health if health in HEALTH else "none",
    }
