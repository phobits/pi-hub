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
                if action == "spotify":
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
