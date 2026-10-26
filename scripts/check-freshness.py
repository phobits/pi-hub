#!/usr/bin/env python3
"""Check how the panel shows a lost backend and how old its data is (#18, #19, #20, #22).

Development aid, not a ship gate: like check-gestures.py this drives
Playwright's WebKit and Chromium, neither of which is the Cog/WPE build on the
panel. A green run means the logic holds in both engines; it is no evidence
about the device.

Usage:
    python -m smart_display.local_server &      # demo server on :8090
    python scripts/check-freshness.py [--url http://127.0.0.1:8090/] [--engines webkit,chromium]
    python scripts/check-freshness.py --self-test [--planted-ref v0.11.4]

Needs: pip install playwright, plus `python -m playwright install webkit
chromium-headless-shell`.

The script answers /api/state itself — the demo payload, rewritten per case,
with a content ETag and real 304s (from a stub server on a free port, because
WebKit's Playwright refuses to fulfill a route with a 3xx) — and can make it hang, fail with 500, send
garbage or refuse the connection. Time runs on `page.clock`: outages, the
midnight re-render and NTP steps take milliseconds, not minutes. The boot config
is pinned to the Pi's poll interval (4 s → the 5 s floor in app.js) and a long
idle timeout, so the screensaver only starts where a case asks for it.

--self-test serves app.js from --planted-ref (the release before #18–#20, which
had no watchdog and plain "HH:MM" stamps) and requires every positive case to
fail against it, so a suite that passes without looking cannot pass the
self-test.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from smart_display.weather_codes import warning_title  # noqa: E402 — the name the server sends
VIENNA = ZoneInfo("Europe/Vienna")
# The page boots from `window.__SMART_DISPLAY__ = {...}`. Patching it through a
# setter keeps the document itself untouched: a document answered by
# route.fulfill counts as a public address space in Chromium, which then blocks
# the loopback stub below (Private Network Access).
BOOT_PATCH_JS = """
(() => {
  const patch = %s;
  let boot;
  Object.defineProperty(window, "__SMART_DISPLAY__", {
    configurable: true,
    get() { return boot; },
    set(value) {
      value.state = patch.state;
      Object.assign(value.config, patch.config);
      boot = value;
    },
  });
})();
"""
# The Pi polls every 4 s (Spotify on), which app.js floors to 5 s.
PI_POLL_SECONDS = 4


def vienna(*args: int) -> datetime:
    return datetime(*args, tzinfo=VIENNA)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# ---- state mutators -----------------------------------------------------------

def weather_behind(updated_at, status: str = "stale"):
    def apply(state: dict) -> dict:
        state["weather"]["snapshot"].update({"status": status, "updated_at": updated_at})
        return state
    return apply


def calendar_behind(updated_at, status: str = "stale"):
    def apply(state: dict) -> dict:
        state["calendar"]["snapshot"].update({"status": status, "updated_at": updated_at})
        return state
    return apply


def spotify_error(updated_at):
    def apply(state: dict) -> dict:
        state["spotify"]["snapshot"].update({"status": "error", "updated_at": updated_at})
        return state
    return apply


def calendar_without_sections(status: str, updated_at=None):
    def apply(state: dict) -> dict:
        state["calendar"].update({"sections": [], "items": []})
        state["calendar"]["snapshot"].update({"status": status, "updated_at": updated_at})
        return state
    return apply


def weather_without_data(status: str):
    def apply(state: dict) -> dict:
        state["weather"].update({"temperature_c": None, "apparent_temperature_c": None,
                                 "condition": "", "condition_code": None, "forecast": []})
        state["weather"]["snapshot"].update({"status": status, "updated_at": None})
        return state
    return apply


def timer_running(now: datetime):
    def apply(state: dict) -> dict:
        t = now.timestamp()
        state["timers"] = [{"id": "t1", "label": "Nudeln", "duration_seconds": 4000,
                            "ends_at": t + 3725, "created_at": t - 275, "dismissed": False}]
        return state
    return apply


def warmer(state: dict) -> dict:
    state["weather"]["temperature_c"] = 5.0
    return state


def compose(*mutators):
    def apply(state: dict) -> dict:
        for mutate in mutators:
            state = mutate(state)
        return state
    return apply


# #22: weather detail and warnings. Times are absolute, like the case clock.

def warning_item(wid: str, *, begins: datetime, ends: datetime, level: int = 2, wtype: int = 1,
                 visible_until: datetime | None = None) -> dict:
    return {"id": wid, "version": 1, "type": wtype, "type_label": "", "level": level,
            "level_label": "", "begins_at": iso(begins), "ends_at": iso(ends),
            "visible_until": iso(visible_until or ends), "title": warning_title(wtype, level),
            "text": "Warnung", "impacts": "",
            "recommendations": "", "meteo_text": ""}


def warnings(items: list, status: str = "ok", updated_at: str | None = None):
    def apply(state: dict) -> dict:
        state["weather_warnings"] = {
            "snapshot": {"status": status, "updated_at": updated_at, "stale_after_seconds": 600,
                         "error_message": None, "source": "geosphere-warnings"},
            "municipality_id": "90101", "municipality_name": "Wien-Innere Stadt", "items": items}
        return state
    return apply


def hourly_series(start: datetime, hours: int, status: str = "ok", updated_at: str | None = None):
    """An hourly C-LAEF-shaped series from ``start`` (UTC hour ends)."""
    def apply(state: dict) -> dict:
        base = start.astimezone(timezone.utc)
        points = [{"time": iso(base + timedelta(hours=h)), "temperature_c": 8.0 + (h % 12),
                   "precipitation_mm": 0.0, "precipitation_probability": None,
                   "wind_speed_kmh": 10.0, "wind_direction_deg": 270.0, "wind_gusts_kmh": 20.0,
                   "icon": "cloudy", "condition": "Bedeckt"} for h in range(hours)]
        state["weather_hourly"] = {
            "snapshot": {"status": status, "updated_at": updated_at or iso(start),
                         "stale_after_seconds": 7200, "error_message": None, "source": "geosphere-claef"},
            "model_run": iso(start - timedelta(hours=6)), "valid_from": points[0]["time"],
            "valid_to": points[-1]["time"], "points": points, "fallback": False,
            "primary_status": status}
        return state
    return apply


def nowcast(start: datetime, amounts: list, status: str = "ok"):
    def apply(state: dict) -> dict:
        base = start.astimezone(timezone.utc)
        state["weather_nowcast"] = {
            "snapshot": {"status": status, "updated_at": iso(start), "stale_after_seconds": 1800,
                         "error_message": None, "source": "geosphere-inca"},
            "model_run": iso(base), "interval_minutes": 15,
            "points": [{"ends_at": iso(base + timedelta(minutes=15 * (i + 1))), "precipitation_mm": a}
                       for i, a in enumerate(amounts)]}
        return state
    return apply


# ---- harness ------------------------------------------------------------------

def encode(state: dict) -> tuple[str, str]:
    body = json.dumps(state, ensure_ascii=False)
    return body, '"' + hashlib.blake2s(body.encode(), digest_size=12).hexdigest() + '"'


class StateStub(ThreadingHTTPServer):
    """Answers the 200/304 path of /api/state with whatever ``current`` holds."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _StubHandler)
        self.current = ("{}", '""')
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/api/state"


class _StubHandler(BaseHTTPRequestHandler):
    # WebKit treats the rerouted request as cross-origin (another port) and
    # preflights it for If-None-Match; app.js reads the ETag, so expose it.
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "If-None-Match")
        self.send_header("Access-Control-Expose-Headers", "ETag")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        body, etag = self.server.current
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self._cors()
            self.send_header("ETag", etag)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


class Run:
    def __init__(self, browser, url: str, base_state: dict, planted_js: str | None,
                 stub: StateStub) -> None:
        self.stub = stub
        self.browser = browser
        self.url = url
        self.base_state = base_state
        self.planted_js = planted_js
        self.context = None
        self.page: Page | None = None
        self.hung: list = []

    async def close(self) -> None:
        # Release the requests "hang" never answered, or their route tasks
        # outlive the context.
        for route in self.hung:
            try:
                await route.abort()
            except Exception:
                pass
        self.hung = []
        if self.context is not None:
            await self.context.close()
            self.context = None

    async def fresh(self, *, now: datetime, mutate=None, browser_zone: str = "Europe/Vienna") -> None:
        await self.close()
        self.mode = "pass"
        self.requests = 0
        self.not_modified = 0
        self.errors: list[str] = []
        self.set_state(mutate)
        self.context = await self.browser.new_context(
            viewport={"width": 1024, "height": 600}, timezone_id=browser_zone, locale="de-AT"
        )
        self.page = page = await self.context.new_page()
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        await page.clock.install(time=now)
        await page.add_init_script(BOOT_PATCH_JS % json.dumps({
            "state": self.state,
            "config": {"state_etag": self.etag, "poll_interval_seconds": PI_POLL_SECONDS,
                       "idle_timeout_seconds": 10**6},
        }))
        await page.route("**/api/state", self._state_route)
        await page.route("**/api/screensaver/next", lambda r, _q: r.fulfill(json={"image": None}))
        if self.planted_js is not None:
            await page.route("**/static/js/app.js*", lambda r, _q: r.fulfill(
                body=self.planted_js, content_type="application/javascript"))
        await page.goto(self.url)
        await page.wait_for_function("document.getElementById('calendar-list').children.length > 0")

    def set_state(self, mutate=None) -> None:
        state = copy.deepcopy(self.base_state)
        self.state = mutate(state) if mutate else state
        self.body, self.etag = encode(self.state)
        self.stub.current = (self.body, self.etag)

    async def _state_route(self, route, request) -> None:
        self.requests += 1
        mode = self.mode
        if mode == "hang":
            self.hung.append(route)  # never answered; the page has to give up
            return
        if mode == "refused":
            await route.abort("connectionrefused")
        elif mode == "500":
            await route.fulfill(status=500, body="Internal Server Error")
        elif mode == "garbage":
            await route.fulfill(status=200, content_type="application/json", body="{nicht json")
        elif mode == "wrong-shape":
            await route.fulfill(status=200, json={"ok": True})
        else:
            if request.headers.get("if-none-match") == self.etag:
                self.not_modified += 1
            await route.continue_(url=self.stub.url)

    async def advance(self, seconds: float) -> None:
        """Fake time forward in 1 s steps, with a real pause per step so a
        fetch started in one step can settle before the next."""
        for _ in range(int(seconds)):
            await self.page.clock.run_for(1000)
            await self.page.wait_for_timeout(15)

    async def text(self, selector: str) -> str:
        return (await self.page.text_content(selector) or "").strip()

    async def shown(self, selector: str) -> bool:
        return await self.page.evaluate(
            "(s) => { const n = document.querySelector(s); return !!n && !n.classList.contains('is-hidden'); }",
            selector)

    async def rail_note(self) -> str:
        return await self.text("#status-note") if await self.shown("#status-note") else ""

    async def start_screensaver(self) -> None:
        await self.page.click("#sheet-handle")
        await self.page.click("#screensaver-start")
        await self.page.wait_for_timeout(100)

    async def to_weather(self) -> None:
        """Two left swipes with the mouse (pointer events), home → weather."""
        for _ in range(2):
            await self.page.mouse.move(820, 300)
            await self.page.mouse.down()
            for step in range(1, 7):
                await self.page.mouse.move(820 - step * 80, 300)
            await self.page.mouse.up()
            await self.page.wait_for_timeout(60)
        if await self.page.evaluate("document.getElementById('screen-stage').dataset.activeScreen") != "weather":
            raise RuntimeError("did not reach the weather screen")

    async def minutes(self, count: int) -> None:
        for _ in range(count):
            await self.page.clock.run_for(60_000)
            await self.page.wait_for_timeout(5)

    async def warning_line(self) -> str:
        return await self.text("#screensaver-warning-text") if await self.shown("#screensaver-warning") else ""


LOST = "Anzeige wird aktualisiert"
NOW = vienna(2026, 10, 25, 12, 0)   # the CEST → CET day itself
CASES: list = []


def case(name: str, positive: bool = True):
    """``positive`` cases need the new behaviour to pass, so the planted old
    app.js has to fail them; the others only rule out false alarms."""
    def register(fn):
        CASES.append((name, positive, fn))
        return fn
    return register


# ---- #18: lost local backend -------------------------------------------------------

@case("Dienst weg: Daten bleiben stehen, Hinweis erst nach der Frist")
async def _gone(run: Run):
    await run.fresh(now=NOW)
    run.mode = "refused"
    await run.advance(20)
    early = await run.rail_note()
    await run.advance(20)
    late = await run.rail_note()
    temperature = await run.text("#weather-temperature")
    agenda = await run.page.locator("#calendar-list .agenda__item, #calendar-list .agenda__next").count()
    if early or late != LOST or temperature in {"", "–", "--"} or agenda == 0:
        return f"after 20 s {early!r}, after 40 s {late!r}, temp {temperature!r}, rows {agenda}"


@case("Hängende Anfrage: Timeout greift, ein späterer Versuch folgt")
async def _hang(run: Run):
    await run.fresh(now=NOW)
    run.mode = "hang"
    await run.advance(21)   # poll at 5 s hangs, abort at 15 s, next poll at 20 s
    attempts = run.requests
    await run.advance(25)
    note = await run.rail_note()
    if attempts < 2 or note != LOST:
        return f"{attempts} request(s) after 21 s, rail {note!r} after 46 s"


@case("HTTP 500: kein falscher Erfolg, kein Absturz")
async def _http_error(run: Run):
    await run.fresh(now=NOW)
    run.mode = "500"
    await run.advance(40)
    note = await run.rail_note()
    if note != LOST or run.errors:
        return f"rail {note!r}, page errors {run.errors}"


@case("Ungültiges JSON und falsche Payload: kein falscher Erfolg, kein Absturz")
async def _garbage(run: Run):
    for mode in ("garbage", "wrong-shape"):
        await run.fresh(now=NOW)
        run.mode = mode
        await run.advance(40)
        note = await run.rail_note()
        temperature = await run.text("#weather-temperature")
        if note != LOST or run.errors or temperature in {"", "–", "--"}:
            return f"{mode}: rail {note!r}, temp {temperature!r}, page errors {run.errors}"


@case("Dienst zurück: Aktualisierung ohne Reload, Hinweis verschwindet")
async def _back(run: Run):
    await run.fresh(now=NOW)
    run.mode = "refused"
    await run.advance(40)
    lost = await run.rail_note()
    run.mode = "pass"
    run.set_state(warmer)
    await run.advance(6)
    note = await run.rail_note()
    temperature = await run.text("#weather-temperature")
    if lost != LOST or note or temperature != "5°":
        return f"lost {lost!r}, after return rail {note!r}, temp {temperature!r}"


@case("Wiederholte 304: kein Fehlalarm", positive=False)
async def _not_modified(run: Run):
    await run.fresh(now=NOW)
    await run.advance(180)
    note = await run.rail_note()
    if note or run.not_modified < 20:
        return f"rail {note!r}, {run.not_modified} × 304 of {run.requests}"


# #22 changed this deliberately: the screensaver shows official warnings, so
# it polls every 5 min instead of not at all (≤ 24 polls in 2 h, not ~1400).
# Loose bounds: the fake clock jumps a minute per step and each awaited poll
# lets the cadence slip by real time, so the count varies (13–24 seen). The
# floor (one poll per 15 min) still catches a cadence that broke or stretched.
# Those polls never feed the lost-link hint — the second hour has no backend.
@case("Zwei Stunden Screensaver: langsamer Poll, kein Fehlalarm, danach wird ein Ausfall erkannt")
async def _screensaver(run: Run):
    await run.fresh(now=NOW)
    await run.start_screensaver()
    before = run.requests
    await run.minutes(60)
    run.mode = "refused"
    await run.minutes(60)
    polled = run.requests - before
    await run.page.keyboard.press("Escape")
    await run.page.wait_for_timeout(100)
    await run.advance(3)
    right_after = await run.rail_note()
    await run.advance(40)
    later = await run.rail_note()
    if not 8 <= polled <= 25 or right_after or later != LOST:
        return f"{polled} polls during pause, rail {right_after!r} on wake, {later!r} 40 s later"


@case("Wetterprovider bleibt fehlerhaft: Wiederverbindung nimmt nur den lokalen Hinweis weg")
async def _weather_still_broken(run: Run):
    await run.fresh(now=NOW, mutate=weather_behind(iso(vienna(2026, 10, 25, 9, 30))))
    run.mode = "refused"
    await run.advance(40)
    lost = await run.rail_note()
    run.mode = "pass"
    await run.advance(6)
    note = await run.rail_note()
    stamp = await run.text("#weather-stamp")
    if lost != LOST or note or stamp != "Stand 09:30":
        return f"lost {lost!r}, after return rail {note!r}, weather {stamp!r}"


@case("NTP-Sprung vor und zurück: kein Fehlalarm", positive=False)
async def _ntp(run: Run):
    await run.fresh(now=NOW)
    await run.advance(10)
    await run.page.clock.set_system_time(NOW + timedelta(hours=6))
    await run.advance(10)
    await run.page.clock.set_system_time(NOW - timedelta(hours=3))
    run.mode = "refused"   # one failed poll right after a step must not count it in
    await run.advance(6)
    note = await run.rail_note()
    if note:
        return f"rail {note!r}"


# ---- #20: age and source -------------------------------------------------------------

AGE_CASES = [
    # (label, positive, now, updated_at, expected weather stamp)
    ("heute", False, NOW, iso(vienna(2026, 10, 25, 9, 30)), "Stand 09:30"),
    ("gestern", True, NOW, iso(vienna(2026, 10, 24, 15, 10)), "Stand gestern 15:10"),
    ("vor 48 h", True, NOW, iso(vienna(2026, 10, 23, 12, 0)), "Stand 23. Okt. 12:00"),
    ("Vorjahr", True, NOW, iso(vienna(2025, 10, 24, 15, 10)), "Stand 24. Okt. 2025 15:10"),
    ("fehlender Zeitstempel", False, NOW, None, ""),
    ("ungültiger Zeitstempel", False, NOW, "kaputt", ""),
    # 25 Oct 2026, 03:00 CEST → 02:00 CET: calendar days, not 24-h blocks.
    ("Herbst-Umstellung, Nacht davor", True, NOW, "2026-10-24T21:30:00+00:00", "Stand gestern 23:30"),
    ("Herbst-Umstellung, nach Mitternacht", False, NOW, "2026-10-24T22:30:00+00:00", "Stand 00:30"),
    ("Frühjahrs-Umstellung, Nacht davor", True, vienna(2026, 3, 29, 12, 0),
     "2026-03-28T22:30:00+00:00", "Stand gestern 23:30"),
]


def _age_case(label, now, updated_at, expected):
    async def check(run: Run):
        await run.fresh(now=now, mutate=weather_behind(updated_at))
        stamp = await run.text("#weather-stamp")
        if stamp != expected or run.errors:
            return f"{stamp!r} != {expected!r} {run.errors or ''}"
    return check


for _label, _positive, _now, _updated, _expected in AGE_CASES:
    case(f"Datenalter: {_label}", positive=_positive)(_age_case(_label, _now, _updated, _expected))


@case("Mitternacht: aus „Stand 20:00“ wird ohne neue Daten „gestern 20:00“")
async def _midnight(run: Run):
    await run.fresh(now=vienna(2026, 10, 24, 23, 59, 0),
                    mutate=weather_behind(iso(vienna(2026, 10, 24, 20, 0))))
    before = await run.text("#weather-stamp")
    await run.advance(70)
    after = await run.text("#weather-stamp")
    if (before, after) != ("Stand 20:00", "Stand gestern 20:00"):
        return f"{before!r} → {after!r}"


@case("Mitternacht zählt in TIMEZONE, auch wenn der Browser in UTC läuft")
async def _midnight_foreign_zone(run: Run):
    await run.fresh(now=vienna(2026, 10, 24, 23, 59, 0), browser_zone="UTC",
                    mutate=weather_behind(iso(vienna(2026, 10, 24, 20, 0))))
    await run.advance(70)
    after = await run.text("#weather-stamp")
    if after != "Stand gestern 20:00":
        return repr(after)


@case("Jahreswechsel: gestern bleibt gestern, älteres bekommt das Jahr")
async def _new_year(run: Run):
    eve = vienna(2026, 12, 31, 23, 59, 0)
    results = []
    for stamp, expected in ((vienna(2026, 12, 31, 10, 0), "Stand gestern 10:00"),
                            (vienna(2026, 12, 30, 10, 0), "Stand 30. Dez. 2026 10:00")):
        await run.fresh(now=eve, mutate=weather_behind(iso(stamp)))
        await run.advance(70)
        results.append((await run.text("#weather-stamp"), expected))
    wrong = [f"{got!r} != {want!r}" for got, want in results if got != want]
    if wrong:
        return "; ".join(wrong)


@case("Musikleiste: Ausfall nennt das Alter im selben Format")
async def _spotify_age(run: Run):
    await run.fresh(now=NOW, mutate=spotify_error(iso(vienna(2026, 10, 23, 12, 0))))
    title, text = await run.text("#spotify-track"), await run.text("#spotify-artist")
    if (title, text) != ("Spotify nicht erreichbar", "zuletzt 23. Okt. 12:00"):
        return f"{title!r} / {text!r}"


@case("Kalenderfehler: Hinweis nennt die Termine, nicht das Gerät")
async def _calendar_rail(run: Run):
    notes = []
    # In another year the year replaces the time (room for the timer chip).
    for stamp, expected in ((vienna(2026, 10, 24, 15, 10), "Termine · gestern 15:10"),
                            (vienna(2026, 10, 12, 15, 10), "Termine · 12. Okt. 15:10"),
                            (vienna(2025, 12, 30, 14, 10), "Termine · 30. Dez. 2025")):
        await run.fresh(now=NOW, mutate=calendar_behind(iso(stamp)))
        notes.append((await run.rail_note(), expected))
    wrong = [f"{got!r} != {want!r}" for got, want in notes if got != want]
    if wrong:
        return "; ".join(wrong)


@case("Wetterfehler: Alter steht am Wetter, nicht doppelt in der Statusleiste")
async def _weather_once(run: Run):
    await run.fresh(now=NOW, mutate=weather_behind(iso(vienna(2026, 10, 24, 15, 10))))
    note = await run.rail_note()
    stamp = await run.text("#weather-stamp")
    if note or stamp != "Stand gestern 15:10":
        return f"rail {note!r}, weather {stamp!r}"


@case("Screensaver: veraltetes Wetter fehlt, aktuelles steht da")
async def _screensaver_weather(run: Run):
    await run.fresh(now=NOW, mutate=weather_behind(iso(vienna(2026, 10, 25, 9, 30))))
    await run.start_screensaver()
    stale = await run.text("#screensaver-weather")
    await run.fresh(now=NOW)
    await run.start_screensaver()
    fresh = await run.text("#screensaver-weather")
    if stale or "°" not in fresh:
        return f"stale {stale!r}, fresh {fresh!r}"


@case("Statusleiste: lokaler Hinweis vor Kalenderhinweis, Timer bleibt, nichts überlappt")
async def _precedence(run: Run):
    await run.fresh(now=NOW, mutate=compose(calendar_behind(iso(vienna(2026, 10, 12, 15, 10))),
                                             timer_running(NOW)))
    calendar_note = await run.rail_note()
    calendar_clipped = await run.page.evaluate(
        "() => { const n = document.getElementById('status-note'); return n.scrollWidth > n.clientWidth; }")
    run.mode = "refused"
    await run.advance(40)
    note = await run.rail_note()
    boxes = await run.page.evaluate("""() => ['#status-rail', '#status-note', '#timer-chip']
        .map((s) => document.querySelector(s).getBoundingClientRect())
        .map((r) => [r.left, r.right, r.width])""")
    (rail_l, rail_r, _), (note_l, note_r, note_w), (chip_l, chip_r, _) = boxes
    fits = note_w > 0 and rail_l - 0.5 <= note_l and note_r <= chip_l + 0.5 and chip_r <= rail_r + 0.5
    clipped = await run.page.evaluate(
        "() => { const n = document.getElementById('status-note'); return n.scrollWidth > n.clientWidth; }")
    if calendar_note != "Termine · 12. Okt. 15:10" or note != LOST or calendar_clipped or clipped \
            or not await run.shown("#timer-chip") or not fits:
        return (f"calendar {calendar_note!r} clipped={calendar_clipped}, then {note!r} clipped={clipped}, "
                f"chip {await run.shown('#timer-chip')}, boxes {boxes}")


# ---- #22: warnings and the weather screen -------------------------------------------------

@case("Warnung endet ohne neuen Abruf pünktlich")
async def _warning_expires(run: Run):
    item = warning_item("w", begins=NOW - timedelta(hours=1), ends=NOW + timedelta(minutes=10))
    await run.fresh(now=NOW, mutate=warnings([item], updated_at=iso(NOW)))
    await run.start_screensaver()
    before = await run.warning_line()
    await run.minutes(9)
    still = await run.warning_line()
    await run.minutes(2)
    after = await run.warning_line()
    if not before.startswith("Orange Windwarnung") or not still or after:
        return f"{before!r} → {still!r} → {after!r}"


@case("Künftige Warnung erscheint erst innerhalb von 24 Stunden")
async def _warning_future(run: Run):
    item = warning_item("w", begins=NOW + timedelta(hours=24, minutes=30), ends=NOW + timedelta(hours=30),
                        level=1, wtype=3)
    await run.fresh(now=NOW, mutate=warnings([item], updated_at=iso(NOW)))
    await run.start_screensaver()
    early = await run.warning_line()
    await run.minutes(31)
    later = await run.warning_line()
    if early or not later.startswith("Gelbe Schneewarnung · ab morgen"):
        return f"{early!r} → {later!r}"


@case("Mehrere Warnungen: die schwerste zuerst, der Rest als +N")
async def _warning_many(run: Run):
    items = [warning_item("r", begins=NOW + timedelta(hours=2), ends=NOW + timedelta(hours=6), level=3, wtype=5),
             warning_item("o", begins=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=8), level=2),
             warning_item("y", begins=NOW + timedelta(hours=30), ends=NOW + timedelta(hours=40), level=1)]
    await run.fresh(now=NOW, mutate=warnings(items, updated_at=iso(NOW)))
    await run.start_screensaver()
    line = await run.warning_line()
    if line != "Rote Gewitterwarnung · ab 14:00 · +1":
        return repr(line)


@case("Warnabruf scheitert: bekannte Warnung bleibt mit Stand, ohne bekannte kein Alarm")
async def _warning_stale(run: Run):
    item = warning_item("w", begins=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=3))
    await run.fresh(now=NOW, mutate=warnings([item], status="stale",
                                             updated_at=iso(vienna(2026, 10, 25, 9, 30))))
    await run.start_screensaver()
    stale = await run.warning_line()
    await run.fresh(now=NOW, mutate=warnings([], status="error"))
    await run.start_screensaver()
    none = await run.warning_line()
    await run.page.keyboard.press("Escape")
    await run.to_weather()
    hint = await run.text("#wx-warn-title")
    if stale != "Orange Windwarnung · bis 15:00 · Stand 09:30" or none \
            or hint != "Warnungen derzeit nicht verfügbar":
        return f"stale {stale!r}, without data {none!r}, detail {hint!r}"


@case("Entwarnung: ein erfolgreicher leerer Abruf nimmt die Warnung weg, auch im Screensaver")
async def _warning_all_clear(run: Run):
    item = warning_item("w", begins=NOW - timedelta(hours=1), ends=NOW + timedelta(hours=6))
    await run.fresh(now=NOW, mutate=warnings([item], updated_at=iso(NOW)))
    await run.start_screensaver()
    before = await run.warning_line()
    run.set_state(warnings([], updated_at=iso(NOW + timedelta(minutes=5))))
    # The next slow poll is due at 5 min. Around it the clock moves in 1 s
    # steps: a whole minute at once would run the 10 s request timeout out
    # before the real response arrives.
    await run.minutes(4)
    await run.advance(90)
    after = await run.warning_line()
    if not before or after:
        return f"{before!r} → {after!r}"


@case("Warnhinweis: „Keine Warnungen“ nur nach erfolgreichem Abruf")
async def _warning_hint_states(run: Run):
    seen = []
    for mutate in (warnings([], status="empty"), warnings([], status="ok", updated_at=iso(NOW)),
                   warnings([], status="error")):
        await run.fresh(now=NOW, mutate=mutate)
        await run.to_weather()
        seen.append(await run.text("#wx-warn-title"))
    if seen != ["Warnungen werden geladen", "Keine Warnungen", "Warnungen derzeit nicht verfügbar"]:
        return repr(seen)


@case("Wetterscreen: vergangene Stunden fallen ohne neue Daten heraus")
async def _hours_move_on(run: Run):
    start = vienna(2026, 10, 25, 11, 0)
    await run.fresh(now=NOW, mutate=hourly_series(start, 40, updated_at=iso(NOW)))
    await run.to_weather()
    first = await run.page.evaluate("() => document.querySelector('#wx-chart .wx-time').textContent")
    await run.minutes(181)
    later = await run.page.evaluate("() => document.querySelector('#wx-chart .wx-time').textContent")
    if (first, later) != ("12:00", "15:00"):
        return f"{first!r} → {later!r}"


@case("Wetterscreen: der 25-Stunden-Tag der Umstellung hat richtige Zeitmarken")
async def _dst_tomorrow(run: Run):
    day_before = vienna(2026, 10, 24, 12, 0)
    await run.fresh(now=day_before, mutate=hourly_series(day_before, 40, updated_at=iso(day_before)))
    await run.to_weather()
    await run.page.click('[data-wx-range="tomorrow"]')
    labels = await run.page.evaluate(
        "() => [...document.querySelectorAll('#wx-chart .wx-time')].map((n) => n.textContent)")
    span = await run.page.evaluate("""() => {
        const pts = document.querySelector('#wx-chart .wx-line').getAttribute('points').split(' ');
        return [pts.length, parseFloat(pts[0]), parseFloat(pts[pts.length - 1])]; }""")
    want = ["00:00", "03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"]
    if labels != want or span[0] != 26:
        return f"labels {labels}, line {span}"


@case("Wetterscreen: Modellhorizont wird genannt, nicht aufgefüllt")
async def _horizon(run: Run):
    await run.fresh(now=NOW, mutate=hourly_series(vienna(2026, 10, 25, 12, 0), 8, updated_at=iso(NOW)))
    await run.to_weather()
    text = await run.page.evaluate(
        "() => (document.querySelector('#wx-chart .wx-horizon-text') || {}).textContent || ''")
    if text != "Modell endet 19:00":
        return repr(text)


@case("Wetterscreen: Alter der Stundenwerte steht in der Quellenzeile")
async def _hourly_age(run: Run):
    stamp = iso(vienna(2026, 10, 24, 15, 10))
    await run.fresh(now=NOW, mutate=hourly_series(vienna(2026, 10, 25, 12, 0), 30, status="stale",
                                                   updated_at=stamp))
    await run.to_weather()
    source = await run.text("#wx-source")
    behind = await run.page.evaluate("() => document.getElementById('wx-source').classList.contains('is-behind')")
    if not source.startswith("Stunden: GeoSphere C-LAEF · Lauf 06:00 · Stand gestern 15:10") or not behind:
        return f"{source!r}, behind={behind}"


@case("Regen: „Kein Niederschlag erwartet“ nur bei vollständigen gültigen Werten")
async def _rain_dry(run: Run):
    start = vienna(2026, 10, 25, 11, 45)
    seen = []
    for mutate in (nowcast(start, [0.0] * 12), nowcast(start, [0.0] * 5 + [None] + [0.0] * 6),
                   nowcast(start, [0.0] * 12, status="stale"), nowcast(start, [0.0] * 4)):
        await run.fresh(now=NOW, mutate=mutate)
        await run.to_weather()
        await run.page.click('[data-wx-mode="rain"]')
        seen.append(await run.page.evaluate(
            "() => (document.querySelector('#wx-chart .wx-summary') || {}).textContent || ''"))
    if seen[0] != "Kein Niederschlag erwartet" or any(s == "Kein Niederschlag erwartet" for s in seen[1:]):
        return repr(seen)


# ---- #19: what an empty agenda says ----------------------------------------------------

@case("Agenda ohne Termine sagt, warum")
async def _agenda_states(run: Run):
    expectations = [
        (calendar_without_sections("disabled"), "Kalender nicht eingerichtet"),
        (calendar_without_sections("empty"), "Termine werden geladen"),
        (calendar_without_sections("error"), "Termine nicht verfügbar"),
        (calendar_without_sections("ok", iso(NOW)), "Keine Termine"),
        (calendar_without_sections("stale", iso(vienna(2026, 10, 24, 9, 0))), "Keine Termine"),
    ]
    wrong = []
    for mutate, expected in expectations:
        await run.fresh(now=NOW, mutate=mutate)
        got = await run.page.evaluate(
            "() => document.querySelector('#calendar-list .agenda__empty').firstChild.textContent")
        if got != expected:
            wrong.append(f"{got!r} != {expected!r}")
    for mutate, expected in ((weather_without_data("disabled"), "Wetter nicht eingerichtet"),
                             (weather_without_data("error"), "Wetter nicht verfügbar")):
        await run.fresh(now=NOW, mutate=mutate)
        got = await run.text("#weather-condition")
        if got != expected:
            wrong.append(f"{got!r} != {expected!r}")
    if wrong:
        return "; ".join(wrong)


# ---- runner -------------------------------------------------------------------------------

def base_state(url: str) -> dict:
    with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=10) as response:
        return json.load(response)


async def suite(url: str, engines: list[str], planted_js: str | None):
    results: dict[tuple[str, str], str | None] = {}
    state = base_state(url)
    stub = StateStub()
    async with async_playwright() as p:
        for engine in engines:
            browser = await getattr(p, engine).launch()
            run = Run(browser, url, state, planted_js, stub)
            for name, _positive, fn in CASES:
                try:
                    results[(engine, name)] = await fn(run)
                except Exception as error:  # a crash is a failure, not an abort
                    results[(engine, name)] = f"error: {error}"
            await run.close()
            await browser.close()
    stub.shutdown()
    return results


def report(results, label: str) -> int:
    failed = 0
    for (engine, name), reason in results.items():
        mark = "ok  " if reason is None else "FAIL"
        failed += reason is not None
        print(f"{label}{mark} {engine:8} {name}" + ("" if reason is None else f" — {reason}"))
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8090/")
    parser.add_argument("--engines", default="webkit,chromium")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--planted-ref", default="v0.11.4")
    args = parser.parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    started = time.monotonic()
    clean = asyncio.run(suite(args.url, engines, None))
    failed = report(clean, "")
    if not args.self_test:
        print(f"\n{len(clean) - failed}/{len(clean)} ok ({time.monotonic() - started:.0f} s)")
        return 1 if failed else 0

    old_js = subprocess.run(
        ["git", "show", f"{args.planted_ref}:smart_display/web/static/js/app.js"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    planted = asyncio.run(suite(args.url, engines, old_js))
    positive = {name for name, is_positive, _ in CASES if is_positive}
    missed = [key for key, reason in planted.items() if key[1] in positive and reason is None]
    for engine, name in missed:
        print(f"SELF-TEST: {args.planted_ref} app.js not caught — {engine} {name}")
    total = sum(1 for key in planted if key[1] in positive)
    ok = failed == 0 and not missed
    print(f"\nself-test {'ok' if ok else 'FAILED'}: clean run {len(clean) - failed}/{len(clean)} ok, "
          f"{args.planted_ref} caught by {total - len(missed)}/{total} positive cases "
          f"({time.monotonic() - started:.0f} s)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
