#!/usr/bin/env python3
"""Render the UI at 1024x600 in every face and state we care about.

Development aid, not a ship gate: this drives headless Chromium through
Playwright, while the panel runs WebKit (Cog). Use it to catch layout
regressions before a deploy, then look at the device.

Usage:
    python -m smart_display.local_server &      # demo server on :8090
    python scripts/screenshot-states.py OUT_DIR [--url http://127.0.0.1:8090/]

Needs: pip install playwright, plus a Chromium Playwright can find (set
CHROME_BIN to a chrome/chromium executable to skip the download).

States are produced by rewriting the demo payload on the way to the browser —
both the bootstrap JSON in the HTML and ``/api/state`` — so the screenshots
cover stale/error/empty branches the mock provider never emits.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

FACES = ["flip", "lcd", "pulse", "qlocktwo", "qlocktwo-ooe", "analog"]
BOOT_RE = re.compile(r"(window\.__SMART_DISPLAY__ = )(\{.*?\})(;\s*</script>)", re.S)
STALE_AT = "2026-10-15T13:10:00+00:00"
# Last year's stamp: the longest age label ("Stand 30. Dez. 2025 14:10").
OLD_AT = "2025-12-30T13:10:00+00:00"


# ---- state mutators ---------------------------------------------------------

def spotify_idle(state: dict) -> dict:
    state["spotify"].update(
        {
            "is_playing": False,
            "track_title": "",
            "artist_name": "",
            "album_name": "",
            "album_art_url": None,
            "can_control": False,
            "supports_volume": False,
            "progress_ms": None,
            "duration_ms": None,
            "device_name": None,
        }
    )
    return state


def spotify_hidden(state: dict) -> dict:
    state = spotify_idle(state)
    state["spotify"]["connected"] = False
    state["spotify"]["snapshot"]["status"] = "disabled"
    return state


def spotify_error(state: dict) -> dict:
    state["spotify"]["snapshot"].update(
        {"status": "error", "error_message": "HTTP 503", "updated_at": STALE_AT}
    )
    return state


def weather_stale(state: dict) -> dict:
    state["weather"]["snapshot"].update(
        {"status": "stale", "error_message": "timeout", "updated_at": STALE_AT}
    )
    return state


def calendar_stale(state: dict) -> dict:
    state["calendar"]["snapshot"].update(
        {"status": "stale", "error_message": "timeout", "updated_at": STALE_AT}
    )
    return state


def calendar_old(state: dict) -> dict:
    state["calendar"]["snapshot"].update(
        {"status": "error", "error_message": "timeout", "updated_at": OLD_AT}
    )
    return state


def all_year_old(state: dict) -> dict:
    """The longest labels everywhere: weather line, rail and music bar."""
    for name in ("weather", "spotify"):
        state[name]["snapshot"].update({"status": "error", "updated_at": OLD_AT})
    return calendar_old(state)


def first_fetch_failed(state: dict) -> dict:
    """#19: configured sources whose first fetch failed — no good data at all."""
    state = weather_empty(state)
    state["weather"]["snapshot"].update({"status": "error", "updated_at": None})
    state["calendar"].update({"sections": [], "items": []})
    state["calendar"]["snapshot"].update({"status": "error", "updated_at": None})
    state = spotify_idle(state)
    state["spotify"]["connected"] = False
    state["spotify"]["snapshot"].update({"status": "error", "updated_at": None})
    return state


def calendar_empty(state: dict) -> dict:
    state["calendar"]["sections"] = []
    state["calendar"]["items"] = []
    return state


def weather_empty(state: dict) -> dict:
    state["weather"].update(
        {
            "temperature_c": None,
            "apparent_temperature_c": None,
            "condition": "",
            "condition_code": None,
            "forecast": [],
        }
    )
    state["weather"]["snapshot"]["status"] = "empty"
    return state


def time_unsynced(state: dict) -> dict:
    state["system"]["time_synced"] = False
    return state


def timer_running(state: dict) -> dict:
    import time

    now = time.time()
    state["timers"] = [
        {
            "id": "t1",
            "label": "",
            "duration_seconds": 600,
            "ends_at": now + 566,
            "created_at": now - 34,
            "dismissed": False,
        }
    ]
    return state


# ---- weather detail (#22) -----------------------------------------------------
# Warnings are built relative to the real clock: the page compares their
# ``visible_until`` with Date.now(), exactly as on the panel.

LONG_IMPACTS = (
    "* Äste können herabstürzen und Gegenstände herumgewirbelt werden.\n"
    "* Erhöhte Unfallgefahr durch starken Seitenwind auf Brücken und exponierten "
    "Straßenzügen, insbesondere für LKW oder bei Fahrten mit großen Anhängern.\n"
    "* Vereinzelt sind Schäden an Dächern und Gerüsten möglich."
)
LONG_ADVICE = (
    "* Seien Sie in Wäldern, Parks und Alleen achtsam, rechnen Sie mit herabstürzenden Ästen!\n"
    "* Reduzieren Sie im Straßenverkehr auf Brücken und exponierten Straßenzügen die "
    "Geschwindigkeit, vermeiden Sie Überholmanöver und halten Sie Abstand!\n"
    "* Sichern Sie lose Gegenstände auf Balkonen und Terrassen."
)
LONG_METEO = (
    "Mit einer nordwestlichen Strömung lebt im Ostalpenraum der Nordwestwind deutlich auf "
    "und erreicht in den Niederungen Windspitzen zwischen 60 und 80 km/h. Im Bergland werden "
    "zum Teil Sturmböen von über 100 km/h erreicht. Erst in der Nacht auf Mittwoch lässt der "
    "Wind von Westen her allmählich nach."
)


def _iso_in(hours: float) -> str:
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.replace(second=0, microsecond=0).isoformat()


def _warning(wid, level, wtype, begins_h, ends_h, *, long=False):
    text = {1: "Wind", 2: "Regen", 3: "Schnee", 5: "Gewitter", 4: "Glatteis"}[wtype]
    adjective = {1: "Gelbe", 2: "Orange", 3: "Rote"}[level]
    return {
        "id": wid,
        "version": 1,
        "type": wtype,
        "type_label": text,
        "level": level,
        "level_label": {1: "Gelb", 2: "Orange", 3: "Rot"}[level],
        "begins_at": _iso_in(begins_h),
        "ends_at": _iso_in(ends_h),
        "visible_until": _iso_in(ends_h),
        "text": f"{adjective} {text}warnung für Wien-Innere Stadt.",
        "impacts": LONG_IMPACTS if long else "* Äste können herabstürzen.",
        "recommendations": LONG_ADVICE if long else "",
        "meteo_text": LONG_METEO if long else "",
    }


def warn_one(state: dict) -> dict:
    state["weather_warnings"]["items"] = [_warning("w1", 2, 1, -1, 5.5)]
    return state


def warn_many(state: dict) -> dict:
    state["weather_warnings"]["items"] = [
        _warning("w3", 3, 5, 2, 6, long=True),
        _warning("w1", 2, 1, -1, 30, long=True),
        _warning("w2", 1, 2, 20, 40, long=True),
    ]
    return state


def warn_future(state: dict) -> dict:
    state["weather_warnings"]["items"] = [_warning("w4", 1, 3, 9, 20)]
    return state


def warn_stale(state: dict) -> dict:
    state = warn_one(state)
    state["weather_warnings"]["snapshot"].update(
        {"status": "stale", "updated_at": _iso_in(-2), "error_message": "HTTP 503"}
    )
    return state


def detail_stale(state: dict) -> dict:
    """Every detail source failed after good data: ages everywhere."""
    for name in ("weather", "weather_hourly", "weather_nowcast", "weather_warnings"):
        state[name]["snapshot"].update({"status": "stale", "updated_at": _iso_in(-2.5), "error_message": "timeout"})
    return state


def detail_empty(state: dict) -> dict:
    """Never fetched — the first minutes after a cold boot."""
    state = weather_empty(state)
    state["weather"].update(
        {"wind_speed_kmh": None, "wind_direction_deg": None, "wind_gusts_kmh": None,
         "observed_at": None, "icon": ""}
    )
    state["weather"]["snapshot"]["updated_at"] = None
    for name in ("weather_hourly", "weather_nowcast", "weather_warnings"):
        state[name]["snapshot"].update({"status": "empty", "updated_at": None})
        state[name]["points" if name != "weather_warnings" else "items"] = []
        state[name]["model_run"] = None
    return state


def detail_failed(state: dict) -> dict:
    """First fetch failed everywhere: no good data at all."""
    state = detail_empty(state)
    state["weather"]["snapshot"]["status"] = "error"
    for name in ("weather_hourly", "weather_nowcast", "weather_warnings"):
        state[name]["snapshot"]["status"] = "error"
    return state


def detail_partial(state: dict) -> dict:
    """GeoSphere down: the hourly series is Open-Meteo's, INCA and the
    warnings have nothing to show."""
    hourly = state["weather_hourly"]
    hourly["snapshot"]["source"] = "open-meteo"
    hourly["fallback"] = True
    hourly["model_run"] = None
    for index, point in enumerate(hourly["points"]):
        point["precipitation_probability"] = min(90, index * 3)
    state["weather_nowcast"].update({"points": []})
    state["weather_nowcast"]["snapshot"].update({"status": "error", "updated_at": None})
    state["weather_warnings"].update({"items": []})
    state["weather_warnings"]["snapshot"].update({"status": "error", "updated_at": None})
    return state


def nowcast_dry(state: dict) -> dict:
    for point in state["weather_nowcast"]["points"]:
        point["precipitation_mm"] = 0.0
    return state


def hourly_short(state: dict) -> dict:
    """An old model run that ends ten hours from now."""
    hourly = state["weather_hourly"]
    hourly["points"] = hourly["points"][:10]
    hourly["valid_to"] = hourly["points"][-1]["time"]
    return state


def compose(*mutators):
    def apply(state: dict) -> dict:
        for mutator in mutators:
            state = mutator(state)
        return state

    return apply


# name, face, night, mutator, actions
SCENARIOS = [
    *[(f"home-{face}", face, False, None, []) for face in FACES],
    ("home-idle-timer", "flip", False, compose(spotify_idle, timer_running), []),
    ("home-hidden-empty", "flip", False, compose(spotify_hidden, calendar_empty), []),
    ("home-offline", "flip", False, compose(spotify_error, weather_stale, calendar_stale), []),
    # #20: days-old data in every face, so both weather heights carry the age.
    *[
        (f"home-days-old-{face}", face, False,
         compose(spotify_error, weather_stale, calendar_stale), [])
        for face in FACES
    ],
    ("home-first-fetch-failed", "flip", False, first_fetch_failed, []),
    # #18: local poll failing, a running timer and the longest calendar age.
    ("home-local-lost-timer", "flip", False,
     compose(calendar_old, weather_stale, timer_running), ["local-lost"]),
    ("home-calendar-old-timer", "qlocktwo", False, compose(calendar_old, timer_running), []),
    ("home-year-old-timer", "flip", False, compose(all_year_old, timer_running), []),
    ("spotify-error-days", "flip", False, spotify_error, ["spotify"]),
    ("screensaver-photo-stale-timer", "flip", False,
     compose(weather_stale, timer_running), ["screensaver"]),
    ("home-weather-empty-unsynced", "pulse", False, compose(weather_empty, time_unsynced), []),
    ("home-night", "pulse", True, None, []),
    ("home-night-qlocktwo", "qlocktwo", True, None, []),
    ("spotify", "flip", False, None, ["spotify"]),
    ("spotify-idle", "flip", False, spotify_idle, ["spotify"]),
    ("spotify-picker", "flip", False, None, ["spotify", "picker"]),
    ("sheet", "flip", False, timer_running, ["sheet"]),
    ("alarm", "flip", False, None, ["alarm"]),
    ("screensaver-photo", "flip", False, None, ["screensaver"]),
    ("screensaver-fallback", "flip", False, None, ["no-photos", "screensaver"]),
    ("screensaver-night", "flip", True, None, ["screensaver"]),
    # #22: screen 3 and the screensaver warning line.
    ("weather", "flip", False, None, ["weather"]),
    ("weather-today", "flip", False, None, ["weather", "wx-today"]),
    ("weather-tomorrow-rain", "flip", False, None, ["weather", "wx-tomorrow"]),
    ("weather-far-day", "flip", False, None, ["weather", "wx-far"]),
    ("weather-rain", "flip", False, None, ["weather", "wx-rain"]),
    ("weather-rain-dry", "flip", False, nowcast_dry, ["weather", "wx-rain"]),
    ("weather-horizon", "flip", False, hourly_short, ["weather"]),
    ("weather-warning", "flip", False, warn_one, ["weather"]),
    ("weather-warning-future", "flip", False, warn_future, ["weather"]),
    ("weather-warnings-many", "flip", False, warn_many, ["weather"]),
    ("weather-warning-sheet", "flip", False, warn_many, ["weather", "wx-warn"]),
    ("weather-warning-sheet-scrolled", "flip", False, warn_many, ["weather", "wx-warn", "wx-scroll"]),
    ("weather-stale", "flip", False, compose(detail_stale, warn_one), ["weather"]),
    ("weather-stale-rain", "flip", False, detail_stale, ["weather", "wx-rain"]),
    ("weather-empty", "flip", False, detail_empty, ["weather"]),
    ("weather-first-fetch-failed", "flip", False, detail_failed, ["weather"]),
    ("weather-first-fetch-failed-rain", "flip", False, detail_failed, ["weather", "wx-rain"]),
    ("weather-partial", "flip", False, detail_partial, ["weather"]),
    ("weather-partial-rain", "flip", False, detail_partial, ["weather", "wx-rain"]),
    ("weather-night", "flip", True, warn_one, ["weather"]),
    ("screensaver-photo-warning", "flip", False, warn_one, ["screensaver"]),
    ("screensaver-photo-warnings-timer", "flip", False, compose(warn_many, timer_running), ["screensaver"]),
    ("screensaver-photo-warning-stale", "flip", False, warn_stale, ["screensaver"]),
    ("screensaver-night-warning", "flip", True, warn_many, ["screensaver"]),
    ("screensaver-night-warning-timer", "flip", True, compose(warn_one, timer_running), ["screensaver"]),
    ("screensaver-fallback-warning", "flip", False, warn_one, ["no-photos", "screensaver"]),
]


def identity(state: dict) -> dict:
    return state


def make_rewrite(mutate, *, idle_fast: bool = False, idle_never: bool = False):
    """Route handler that applies ``mutate`` to the demo state, both in the
    bootstrap JSON of the page and in every ``/api/state`` poll. ``idle_fast``
    drops the screensaver idle timeout to its 5 s floor; ``idle_never`` keeps
    it from firing while fake time is fast-forwarded."""

    async def rewrite(route, request):
        response = await route.fetch()
        if request.url.endswith("/api/state"):
            data = mutate(copy.deepcopy(await response.json()))
            await route.fulfill(response=response, json=data)
            return
        body = await response.text()

        def patch(match: re.Match) -> str:
            boot = json.loads(match.group(2))
            boot["state"] = mutate(copy.deepcopy(boot["state"]))
            if idle_fast:
                boot["config"]["idle_timeout_seconds"] = 1
            if idle_never:
                boot["config"]["idle_timeout_seconds"] = 10**6
            return match.group(1) + json.dumps(boot) + match.group(3)

        await route.fulfill(response=response, body=BOOT_RE.sub(patch, body, count=1))

    return rewrite


async def run(url: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    launch: dict = {"args": ["--no-sandbox"]}
    if os.environ.get("CHROME_BIN"):
        launch["executable_path"] = os.environ["CHROME_BIN"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch)
        for name, face, night, mutate, actions in SCENARIOS:
            context = await browser.new_context(
                viewport={"width": 1024, "height": 600}, device_scale_factor=1
            )
            await context.add_init_script(
                f"localStorage.setItem('sd.watch_face', '{face}');"
                f"localStorage.setItem('sd.night_mode', '{'on' if night else 'off'}');"
            )
            page = await context.new_page()

            if mutate is not None or "screensaver" in actions:
                rewrite = make_rewrite(
                    mutate or identity,
                    idle_fast="screensaver" in actions,
                    idle_never="local-lost" in actions,
                )
                await page.route("**/api/state", rewrite)
                await page.route(url, rewrite)
            if "no-photos" in actions:
                await page.route(
                    "**/api/screensaver/next",
                    lambda route, request: route.fulfill(json={"image": None}),
                )

            if "local-lost" in actions:
                await page.clock.install()
            await page.goto(url)
            await page.wait_for_timeout(1800)
            for action in actions:
                if action == "weather":
                    # Two real swipes, home → Spotify → weather.
                    for _ in range(2):
                        await page.mouse.move(820, 300)
                        await page.mouse.down()
                        for step in range(1, 9):
                            await page.mouse.move(820 - step * 60, 300)
                        await page.mouse.up()
                        await page.wait_for_timeout(350)
                elif action == "wx-today":
                    await page.click('[data-wx-range="today"]')
                elif action == "wx-tomorrow":
                    await page.click('[data-wx-range="tomorrow"]')
                elif action == "wx-far":
                    await page.click(".wx-week__day:nth-child(5)")
                elif action == "wx-rain":
                    await page.click('[data-wx-mode="rain"]')
                elif action == "wx-warn":
                    await page.click("#wx-warn")
                elif action == "wx-scroll":
                    await page.evaluate("document.getElementById('warn-sheet-body').scrollTop = 99999")
                elif action == "spotify":
                    await page.click("#spotify-card")
                elif action == "picker":
                    await page.click("#spotify-detail-open-picker")
                elif action == "sheet":
                    await page.click("#sheet-handle")
                elif action == "screensaver":
                    # 5 s is the floor resetIdleTimer enforces; photos crossfade 1.2 s.
                    await page.wait_for_timeout(7500)
                elif action == "local-lost":
                    # The backend stops answering; fake time runs past the
                    # hint threshold (3 polls, at least 30 s — app.js).
                    await page.unroute("**/api/state")
                    await page.route("**/api/state", lambda route, request: route.abort())
                    for _ in range(100):
                        await page.clock.run_for(1000)
                        await page.wait_for_timeout(10)
                elif action == "alarm":
                    await page.evaluate(
                        "document.getElementById('timer-alarm').classList.add('is-active')"
                    )
                await page.wait_for_timeout(600)
            await page.screenshot(path=str(out / f"{name}.png"))
            await context.close()
        await browser.close()
    print(f"{len(SCENARIOS)} screenshots in {out}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    out = Path(args[0])
    url = "http://127.0.0.1:8090/"
    if "--url" in args:
        url = args[args.index("--url") + 1]
    asyncio.run(run(url, out))


if __name__ == "__main__":
    main()
