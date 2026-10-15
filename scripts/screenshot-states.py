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
    state["spotify"]["snapshot"]["status"] = "empty"
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


def make_rewrite(mutate, *, idle_fast: bool = False):
    """Route handler that applies ``mutate`` to the demo state, both in the
    bootstrap JSON of the page and in every ``/api/state`` poll. ``idle_fast``
    drops the screensaver idle timeout to its 5 s floor."""

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
                rewrite = make_rewrite(mutate or identity, idle_fast="screensaver" in actions)
                await page.route("**/api/state", rewrite)
                await page.route(url, rewrite)
            if "no-photos" in actions:
                await page.route(
                    "**/api/screensaver/next",
                    lambda route, request: route.fulfill(json={"image": None}),
                )

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
