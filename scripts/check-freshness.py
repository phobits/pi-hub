#!/usr/bin/env python3
"""Check how the panel shows a lost backend and how old its data is (#18, #19, #20).

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


@case("Zwei Stunden Screensaver: kein Fehlalarm, danach wird ein Ausfall wieder erkannt")
async def _screensaver(run: Run):
    await run.fresh(now=NOW)
    await run.start_screensaver()
    before = run.requests
    for _ in range(120):
        await run.page.clock.run_for(60_000)
        await run.page.wait_for_timeout(5)
    polled = run.requests - before
    run.mode = "refused"
    await run.page.keyboard.press("Escape")
    await run.page.wait_for_timeout(100)
    await run.advance(3)
    right_after = await run.rail_note()
    await run.advance(40)
    later = await run.rail_note()
    if polled or right_after or later != LOST:
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
