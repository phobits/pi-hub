import asyncio, sys, pathlib
from playwright.async_api import async_playwright
M = pathlib.Path(sys.argv[1]); OUT = pathlib.Path(sys.argv[2]); OUT.mkdir(exist_ok=True)
SHOTS = [
 ("v2-home-flip", "home.html?face=flip"),
 ("v2-home-pulse", "home.html?face=pulse"),
 ("v2-home-lcd", "home.html?face=lcd"),
 ("v2-home-qlocktwo", "home.html?face=qlocktwo"),
 ("v2-home-qlocktwo-ooe", "home.html?face=qlocktwo-ooe"),
 ("v2-home-analog", "home.html?face=analog"),
 ("v2-home-idle-timer", "home.html?face=flip&music=idle&state=timer"),
 ("v2-home-night", "home.html?face=pulse&night=1"),
 ("v2-home-empty", "home.html?face=flip&state=empty&music=idle"),
 ("v2-home-offline", "home.html?face=flip&state=offline&music=error"),
 ("v2-home-stale-sync", "home.html?face=pulse&state=stale&music=playing"),
 ("v2-spotify", "spotify.html"),
 ("v2-sheet", "sheet.html"),
]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome", args=["--no-sandbox"])
        c = await b.new_context(viewport={"width":1024,"height":600}, device_scale_factor=1)
        pg = await c.new_page()
        for name, rel in SHOTS:
            await pg.goto(f"file://{M}/{rel}"); await pg.wait_for_timeout(400)
            await pg.screenshot(path=str(OUT/f"{name}.png"))
            if name == "v2-home-flip":
                await pg.screenshot(path=str(M/"home.png"))
        await b.close()
asyncio.run(main())
