#!/usr/bin/env python3
"""Check the touch gestures of the stage and the screensaver in WebKit and Chromium.

Development aid, not a ship gate: like screenshot-states.py this drives
Playwright browsers, and neither is the Cog/WPE build on the panel. WebKit is
the closer of the two, which is why it runs first. A green run means the
gesture logic holds in both engines; it is no evidence about the device.

Usage:
    python -m smart_display.local_server &      # demo server on :8090
    python scripts/check-gestures.py [--url http://127.0.0.1:8090/] [--engines webkit,chromium]
    python scripts/check-gestures.py --self-test

Needs: pip install playwright, plus `python -m playwright install webkit
chromium-headless-shell`.

Swipes are dispatched as synthetic events, once per event family: pointer
only, touch only, and both interleaved the way real engines emit them (the
code must act once, not twice). WebKit has no Touch constructor, so touch
events are plain Events carrying `touches`/`changedTouches` objects — enough
for app.js, which only reads identifier and coordinates. Taps use
`page.touchscreen.tap`, i.e. real touch input with the engine's own click
synthesis, because click-through is part of what is being checked.

--self-test plants a fault (a capture listener on window that swallows every
pointer and touch event, while clicks and keys still work) and requires every
gesture case to fail, so a suite that passes without looking at the gestures
cannot pass the self-test.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from playwright.async_api import Page, async_playwright

FAMILIES = ("pointer", "touch", "both")

# One gesture = a list of points, start first. Dispatches on `selector`.
GESTURE_JS = """
async ({ selector, family, points, cancel, touchId }) => {
  const el = document.querySelector(selector);
  const id = touchId ?? 11;
  const fakeTouch = (type, x, y) => {
    const t = { identifier: id, target: el, clientX: x, clientY: y };
    const ev = new Event(type, { bubbles: true, cancelable: true });
    const live = type === "touchend" || type === "touchcancel" ? [] : [t];
    Object.defineProperty(ev, "touches", { value: live });
    Object.defineProperty(ev, "changedTouches", { value: [t] });
    el.dispatchEvent(ev);
  };
  const pointer = (type, x, y) => el.dispatchEvent(new PointerEvent(type, {
    pointerId: id, isPrimary: true, button: 0, pointerType: "touch",
    clientX: x, clientY: y, bubbles: true, cancelable: true,
  }));
  const step = (kind, x, y) => {
    const names = {
      down: ["pointerdown", "touchstart"],
      move: ["pointermove", "touchmove"],
      up: cancel ? ["pointercancel", "touchcancel"] : ["pointerup", "touchend"],
    }[kind];
    if (family !== "touch") pointer(names[0], x, y);
    if (family !== "pointer") fakeTouch(names[1], x, y);
  };
  const [first, ...rest] = points;
  step("down", first[0], first[1]);
  rest.forEach(([x, y]) => step("move", x, y));
  const last = points[points.length - 1];
  step("up", last[0], last[1]);
}
"""

# Free-form event sequence for the cases the family helper cannot express.
# Each step: {type, x, y, id} for pointer events, {type, changed: [[id, x, y], ...],
# touches: n} for touch events.
RAW_JS = """
async ({ selector, steps }) => {
  const el = document.querySelector(selector);
  for (const s of steps) {
    if (s.type.startsWith("pointer")) {
      el.dispatchEvent(new PointerEvent(s.type, {
        pointerId: s.id, isPrimary: true, button: 0, pointerType: "touch",
        clientX: s.x, clientY: s.y, bubbles: true, cancelable: true,
      }));
    } else {
      const ev = new Event(s.type, { bubbles: true, cancelable: true });
      const changed = s.changed.map(([identifier, clientX, clientY]) =>
        ({ identifier, target: el, clientX, clientY }));
      Object.defineProperty(ev, "changedTouches", { value: changed });
      Object.defineProperty(ev, "touches", { value: changed.slice(0, s.touches) });
      el.dispatchEvent(ev);
    }
    if (s.wait) await new Promise((r) => setTimeout(r, s.wait));
  }
}
"""

# Planted fault for --self-test: no pointer or touch event reaches the gesture
# trackers. Clicks and keys still work, so the screensaver can still be started
# and a failing case really is a gesture that went unseen, not broken setup.
SWALLOW_JS = """
["pointerdown", "pointermove", "pointerup", "pointercancel",
 "touchstart", "touchmove", "touchend", "touchcancel"].forEach((type) =>
  window.addEventListener(type, (e) => e.stopImmediatePropagation(), true));
"""

SCREEN = "() => document.getElementById('screen-stage').dataset.activeScreen"
SHEET_OPEN = "() => document.getElementById('quick-sheet').classList.contains('is-open')"
SS_ON = "() => document.getElementById('screensaver').classList.contains('is-active')"
SS_MODE = "() => document.getElementById('screensaver').className"
PHOTO = """() => {
  const a = document.querySelector('.screensaver__image.is-active');
  return a ? a.getAttribute('src') : null;
}"""
FACE = "() => document.body.getAttribute('data-watch-face')"
CENTER = """(sel) => { const r = document.querySelector(sel).getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2]; }"""
ENTER_SCREENSAVER = "() => document.getElementById('screensaver-start').click()"
WARN_OPEN = "() => document.getElementById('warn-sheet').classList.contains('is-open')"
FAR_DAY = "() => Boolean(document.querySelector('#wx-chart .wx-day'))"
SELECTED_DAY = "() => (document.querySelector('.wx-week__day.is-selected') || {dataset: {}}).dataset.date || ''"
# #22: three warnings with long texts, written into the boot state before
# app.js reads it (polls are cut off for these cases, so nothing replaces it).
WARNINGS_BOOT_JS = """
(() => {
  let boot;
  const at = (h) => new Date(Date.now() + h * 3600e3).toISOString();
  const long = "* Äste können herabstürzen und Gegenstände herumgewirbelt werden.\\n".repeat(12);
  const warning = (id, level, type, b, e, title) => ({
    id, version: 1, type, level, type_label: "", level_label: "", title,
    begins_at: at(b), ends_at: at(e), visible_until: at(e),
    text: "Warnung", impacts: long, recommendations: long, meteo_text: long,
  });
  Object.defineProperty(window, "__SMART_DISPLAY__", {
    configurable: true,
    get() { return boot; },
    set(value) {
      value.state.weather_warnings = {
        snapshot: { status: "ok", updated_at: at(0), source: "geosphere-warnings" },
        municipality_id: "90101", municipality_name: "Wien-Innere Stadt",
        items: [warning("a", 2, 1, -1, 5, "Orange Windwarnung"),
               warning("b", 1, 2, 2, 8, "Gelbe Regenwarnung"),
               warning("c", 1, 5, 4, 9, "Gelbe Gewitterwarnung")],
      };
      boot = value;
    },
  });
})();
"""


def horizontal(dx: int, y: int = 300, x0: int = 500) -> list[list[int]]:
    return [[x0, y], [x0 + dx // 2, y], [x0 + dx, y]]


class Run:
    def __init__(self, page: Page, url: str, planted: bool) -> None:
        self.page = page
        self.url = url
        self.planted = planted
        self.warnings_script = False

    async def to_weather(self, fam: str = "both") -> None:
        for _ in range(2):
            await self.gesture("#screen-stage", fam, horizontal(-200))

    async def fresh(self, *, night: bool = False, no_photos: bool = False, warnings: bool = False) -> None:
        page = self.page
        await page.unroute("**/api/screensaver/next")
        await page.unroute("**/api/state")
        if no_photos:
            await page.route("**/api/screensaver/next", lambda r: r.fulfill(json={"image": None}))
        if warnings:
            await page.route("**/api/state", lambda r: r.abort())
        await page.goto(self.url)
        await page.evaluate(
            "(mode) => localStorage.setItem('sd.night_mode', mode)", "on" if night else "off"
        )
        if warnings and not self.warnings_script:
            await page.add_init_script(WARNINGS_BOOT_JS)
            self.warnings_script = True
        await page.reload()
        if warnings and not await page.evaluate("Boolean(window.__SMART_DISPLAY__.state.weather_warnings.items.length)"):
            raise RuntimeError("warning injection did not reach the page")
        if self.planted:
            await page.evaluate(SWALLOW_JS)

    async def gesture(self, selector: str, family: str, points, cancel: bool = False) -> None:
        await self.page.evaluate(
            GESTURE_JS,
            {"selector": selector, "family": family, "points": points, "cancel": cancel, "touchId": 0},
        )
        await self.page.wait_for_timeout(450)

    async def screensaver(self) -> None:
        await self.page.evaluate(ENTER_SCREENSAVER)
        await self.page.wait_for_timeout(1200)
        # Every wake case asserts "off" afterwards — meaningless unless it was on.
        if not await self.page.evaluate(SS_ON):
            raise RuntimeError("screensaver did not start")

    async def tap(self, x: float, y: float, wait: int = 450) -> None:
        await self.page.touchscreen.tap(x, y)
        await self.page.wait_for_timeout(wait)

    async def photo_after(self, family: str, dx: int) -> str | None:
        await self.gesture("#screensaver", family, horizontal(dx))
        return await self.page.evaluate(PHOTO)


# Each case returns None when it holds, else a short reason. `positive` marks
# cases that need a gesture to be seen, which the planted fault has to break;
# tap-activates-click and key-wake do not go through the trackers.
CASES: list[tuple[str, bool, object]] = []


def case(name: str, positive: bool = True):
    def register(fn):
        CASES.append((name, positive, fn))
        return fn
    return register


for fam in FAMILIES:
    @case(f"stage: swipe left opens Spotify, right returns ({fam})")
    async def _stage_pages(run: Run, fam=fam):
        await run.fresh()
        await run.gesture("#screen-stage", fam, horizontal(-200))
        first = await run.page.evaluate(SCREEN)
        await run.gesture("#screen-stage", fam, horizontal(200))
        second = await run.page.evaluate(SCREEN)
        if (first, second) != ("spotify", "home"):
            return f"screens {first}, {second}"

    @case(f"stage: swipe up opens the quick sheet ({fam})")
    async def _stage_sheet(run: Run, fam=fam):
        await run.fresh()
        await run.gesture("#screen-stage", fam, [[500, 450], [500, 350], [500, 250]])
        if not await run.page.evaluate(SHEET_OPEN):
            return "sheet closed"

    @case(f"stage: cancelled qualified swipe still pages ({fam})")
    async def _stage_cancel(run: Run, fam=fam):
        await run.fresh()
        await run.gesture("#screen-stage", fam, horizontal(-200), cancel=True)
        if await run.page.evaluate(SCREEN) != "spotify":
            return "did not page"

    @case(f"screensaver: swipes page forward and back through history ({fam})")
    async def _ss_pages(run: Run, fam=fam):
        await run.fresh()
        await run.screensaver()
        p0 = await run.page.evaluate(PHOTO)
        p1 = await run.photo_after(fam, -200)
        p2 = await run.photo_after(fam, -200)
        b1 = await run.photo_after(fam, 200)
        b0 = await run.photo_after(fam, 200)
        edge = await run.photo_after(fam, 200)
        fwd = await run.photo_after(fam, -200)
        on = await run.page.evaluate(SS_ON)
        seq = [p0, p1, p2, b1, b0, edge, fwd]
        if not (on and p0 and p1 != p0 and p2 != p1 and b1 == p1 and b0 == p0 and edge == p0 and fwd == p1):
            return f"sequence {seq}, on={on}"


for fam in FAMILIES:
    @case(f"stage: home → Spotify → weather and back, no wrap at either end ({fam})")
    async def _stage_three(run: Run, fam=fam):
        await run.fresh()
        seen = []
        for dx in (-200, -200, -200, 200, 200, 200):
            await run.gesture("#screen-stage", fam, horizontal(dx))
            seen.append(await run.page.evaluate(SCREEN))
        if seen != ["spotify", "weather", "weather", "spotify", "home", "home"]:
            return f"screens {seen}"


@case("weather: taps select day, range and rain view without paging")
async def _wx_taps(run: Run):
    await run.fresh()
    await run.to_weather()
    x, y = await run.page.evaluate(CENTER, ".wx-week__day:nth-child(5)")
    await run.tap(x, y)
    far = await run.page.evaluate(FAR_DAY)
    x, y = await run.page.evaluate(CENTER, '[data-wx-range="tomorrow"]')
    await run.tap(x, y)
    tomorrow = await run.page.evaluate(SELECTED_DAY)
    x, y = await run.page.evaluate(CENTER, '[data-wx-mode="rain"]')
    await run.tap(x, y)
    rain = await run.page.evaluate(
        "() => document.querySelector('[data-wx-mode=\"rain\"]').classList.contains('is-selected')")
    screen = await run.page.evaluate(SCREEN)
    if not (far and tomorrow and rain and screen == "weather"):
        return f"far={far} tomorrow={tomorrow!r} rain={rain} screen={screen}"


@case("weather: a swipe that starts on a day pages and selects nothing")
async def _wx_swipe_over_day(run: Run):
    await run.fresh()
    await run.to_weather()
    x, y = await run.page.evaluate(CENTER, ".wx-week__day:nth-child(5)")
    await run.gesture("#screen-stage", "both", [[x, y], [x + 120, y], [x + 240, y]])
    screen = await run.page.evaluate(SCREEN)
    await run.gesture("#screen-stage", "both", horizontal(-200))
    if screen != "spotify" or await run.page.evaluate(FAR_DAY):
        return f"screen={screen}, far day selected={await run.page.evaluate(FAR_DAY)}"


@case("weather: swipe up opens the quick sheet there too")
async def _wx_sheet(run: Run):
    await run.fresh()
    await run.to_weather()
    await run.gesture("#screen-stage", "both", [[500, 300], [500, 200], [500, 100]])
    if not await run.page.evaluate(SHEET_OPEN) or await run.page.evaluate(SCREEN) != "weather":
        return "no sheet, or the page moved"


@case("weather: warning overlay scrolls and closes without paging")
async def _wx_overlay(run: Run):
    await run.fresh(warnings=True)
    await run.to_weather()
    x, y = await run.page.evaluate(CENTER, "#wx-warn")
    await run.tap(x, y)
    opened = await run.page.evaluate(WARN_OPEN)
    # A horizontal drag and a vertical scroll inside the overlay.
    await run.gesture("#warn-sheet-body", "both", horizontal(-260, y=300))
    await run.page.mouse.move(500, 300)
    await run.page.mouse.wheel(0, 600)
    await run.page.wait_for_timeout(300)
    scrolled = await run.page.evaluate("() => document.getElementById('warn-sheet-body').scrollTop")
    still = await run.page.evaluate(WARN_OPEN) and await run.page.evaluate(SCREEN) == "weather"
    x, y = await run.page.evaluate(CENTER, "#warn-sheet-close")
    await run.tap(x, y)
    closed = not await run.page.evaluate(WARN_OPEN)
    x, y = await run.page.evaluate(CENTER, "#wx-warn")
    await run.tap(x, y)
    await run.tap(40, 560)  # backdrop, over the week bar's first day
    backdrop = not await run.page.evaluate(WARN_OPEN)
    screen = await run.page.evaluate(SCREEN)
    picked = await run.page.evaluate(SELECTED_DAY)
    if not (opened and scrolled > 0 and still and closed and backdrop and screen == "weather" and not picked):
        return (f"opened={opened} scrolled={scrolled} stayed={still} closed={closed} "
                f"backdrop={backdrop} screen={screen} day={picked!r}")


@case("weather: Spotify queue loads only while Spotify is visible")
async def _wx_queue(run: Run):
    queue_calls = []
    handler = lambda request: queue_calls.append(request.url) if "/api/spotify/queue" in request.url else None  # noqa: E731
    run.page.on("request", handler)
    try:
        await run.fresh()
        await run.page.wait_for_timeout(600)
        on_home = len(queue_calls)
        await run.gesture("#screen-stage", "both", horizontal(-200))
        await run.page.wait_for_timeout(600)
        on_spotify = len(queue_calls)
        await run.gesture("#screen-stage", "both", horizontal(-200))
        timers = await run.page.evaluate("() => document.getElementById('screen-stage').dataset.activeScreen")
        await run.page.wait_for_timeout(1500)
        after = len(queue_calls)
    finally:
        run.page.remove_listener("request", handler)
    if not (on_home == 0 and on_spotify >= 1 and after == on_spotify and timers == "weather"):
        return f"home={on_home} spotify={on_spotify} weather={after} screen={timers}"


@case("weather: the screensaver resets to home and closes the overlay")
async def _wx_screensaver_reset(run: Run):
    await run.fresh(warnings=True)
    await run.to_weather()
    x, y = await run.page.evaluate(CENTER, "#wx-warn")
    await run.tap(x, y)
    if await run.page.evaluate(SCREEN) != "weather" or not await run.page.evaluate(WARN_OPEN):
        return "never reached the weather screen with the overlay open"
    await run.screensaver()
    await run.page.keyboard.press("Escape")
    await run.page.wait_for_timeout(300)
    screen = await run.page.evaluate(SCREEN)
    if screen != "home" or await run.page.evaluate(WARN_OPEN):
        return f"screen={screen}, overlay open={await run.page.evaluate(WARN_OPEN)}"


@case("screensaver: the warning line is not a wake target", positive=False)
async def _ss_warning_line(run: Run):
    await run.fresh(warnings=True)
    await run.screensaver()
    shown = await run.page.evaluate("() => !document.getElementById('screensaver-warning').classList.contains('is-hidden')")
    x, y = await run.page.evaluate(CENTER, "#screensaver-warning")
    await run.tap(x, y)
    if not shown or not await run.page.evaluate(SS_ON):
        return f"shown={shown}, woke on a tap on the warning"


@case("stage: touch end found by identifier, not changedTouches[0]")
async def _stage_identifier(run: Run):
    await run.fresh()
    await run.page.evaluate(RAW_JS, {"selector": "#screen-stage", "steps": [
        {"type": "touchstart", "changed": [[5, 500, 300]], "touches": 1},
        {"type": "touchmove", "changed": [[9, 100, 100], [5, 400, 300]], "touches": 2},
        {"type": "touchend", "changed": [[9, 100, 100], [5, 300, 300]], "touches": 0},
    ]})
    await run.page.wait_for_timeout(450)
    if await run.page.evaluate(SCREEN) != "spotify":
        return "swipe lost behind the other finger"


@case("stage: a pointer gesture whose end never came does not lock out touch")
async def _stage_stale_lock(run: Run):
    await run.fresh()
    await run.page.evaluate(RAW_JS, {"selector": "#screen-stage", "steps": [
        {"type": "pointerdown", "x": 500, "y": 300, "id": 3, "wait": 1700},
        {"type": "touchstart", "changed": [[6, 500, 300]], "touches": 1},
        {"type": "touchmove", "changed": [[6, 400, 300]], "touches": 1},
        {"type": "touchend", "changed": [[6, 300, 300]], "touches": 0},
    ]})
    await run.page.wait_for_timeout(450)
    if await run.page.evaluate(SCREEN) != "spotify":
        return "touch family still locked out"


@case("stage: short drag does nothing", positive=False)
async def _stage_short(run: Run):
    await run.fresh()
    await run.gesture("#screen-stage", "both", horizontal(-40))
    if await run.page.evaluate(SCREEN) != "home":
        return "paged"


@case("stage: drag on a slider does not page", positive=False)
async def _stage_slider(run: Run):
    await run.fresh()
    await run.gesture("#screen-stage", "both", horizontal(-200))
    await run.gesture("#spotify-detail-volume", "both", horizontal(200, y=0, x0=0))
    if await run.page.evaluate(SCREEN) != "spotify":
        return "paged off the Spotify screen"


@case("stage: tap on the music bar still activates it", positive=False)
async def _stage_tap(run: Run):
    await run.fresh()
    x, y = await run.page.evaluate(CENTER, "#spotify-card")
    await run.tap(x, y)
    if await run.page.evaluate(SCREEN) != "spotify":
        return "tap swallowed"


@case("screensaver: tap on the photo keeps it, tap on the clock wakes")
async def _ss_clock(run: Run):
    await run.fresh()
    await run.screensaver()
    await run.tap(700, 200)
    stayed = await run.page.evaluate(SS_ON)
    x, y = await run.page.evaluate(CENTER, "#screensaver-info")
    await run.tap(x, y)
    if not stayed or await run.page.evaluate(SS_ON):
        return f"stayed={stayed} after photo tap; clock tap did not wake"


@case("screensaver: double tap wakes")
async def _ss_double(run: Run):
    await run.fresh()
    await run.screensaver()
    await run.tap(700, 200, wait=120)
    await run.tap(704, 203, wait=300)
    if await run.page.evaluate(SS_ON):
        return "still on"


@case("screensaver: at night any tap wakes")
async def _ss_night(run: Run):
    await run.fresh(night=True)
    await run.screensaver()
    mode = await run.page.evaluate(SS_MODE)
    await run.tap(1000, 20)
    if "screensaver--night" not in mode or await run.page.evaluate(SS_ON):
        return f"mode {mode!r}, still on"


@case("screensaver: without photos only the clock wakes, without click-through")
async def _ss_fallback(run: Run):
    await run.fresh(no_photos=True)
    await run.screensaver()
    await run.tap(1000, 20)
    stayed = await run.page.evaluate(SS_ON)
    before = await run.page.evaluate(FACE)
    x, y = await run.page.evaluate(CENTER, "#watch-face")
    await run.tap(x, y)  # the flip clock covers #watch-face
    woke = not await run.page.evaluate(SS_ON)
    after = await run.page.evaluate(FACE)
    await run.tap(x, y)  # the next real tap must reach the face again
    later = await run.page.evaluate(FACE)
    if not (stayed and woke and after == before and later != after):
        return f"stayed={stayed} woke={woke} face {before}->{after}->{later}"


@case("screensaver: a key wakes", positive=False)
async def _ss_key(run: Run):
    await run.fresh()
    await run.screensaver()
    await run.page.keyboard.press("Escape")
    await run.page.wait_for_timeout(200)
    if await run.page.evaluate(SS_ON):
        return "still on"


async def suite(url: str, engines: list[str], planted: bool) -> dict[tuple[str, str], str | None]:
    results: dict[tuple[str, str], str | None] = {}
    async with async_playwright() as p:
        for engine in engines:
            browser = await getattr(p, engine).launch()
            context = await browser.new_context(viewport={"width": 1024, "height": 600}, has_touch=True)
            run = Run(await context.new_page(), url, planted)
            for name, _positive, fn in CASES:
                try:
                    results[(engine, name)] = await fn(run)
                except Exception as error:  # a crash is a failure, not an abort
                    results[(engine, name)] = f"error: {error}"
            await browser.close()
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
    args = parser.parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    clean = asyncio.run(suite(args.url, engines, planted=False))
    failed = report(clean, "")
    if not args.self_test:
        print(f"\n{len(clean) - failed}/{len(clean)} ok")
        return 1 if failed else 0

    planted = asyncio.run(suite(args.url, engines, planted=True))
    positive = {name for name, is_positive, _ in CASES if is_positive}
    missed = [key for key, reason in planted.items() if key[1] in positive and reason is None]
    for engine, name in missed:
        print(f"SELF-TEST: planted fault not detected — {engine} {name}")
    ok = failed == 0 and not missed
    print(f"\nself-test {'ok' if ok else 'FAILED'}: clean run {len(clean) - failed}/{len(clean)} ok, "
          f"planted fault caught by {sum(1 for k in planted if k[1] in positive) - len(missed)}"
          f"/{sum(1 for k in planted if k[1] in positive)} positive cases")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
