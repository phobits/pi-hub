# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context

Smart Display for a Raspberry Pi Zero 2 W driving a 1024x600 touch panel in kiosk mode. **Hard constraints:** 512 MB RAM target, ARM, always-on, no client-side framework, no JS build step on the device. Every change must be justifiable against this hardware.

The full product brief — UI hierarchy, failure-mode rules, what NOT to regress — is in `AGENTS.md`. Read it before making non-trivial UI or provider changes; the hierarchy decisions there are deliberate and several have been re-litigated already.

User-facing strings are German and must use real umlauts.

The UI is set in **Inter** (`fonts-inter`, installed by `scripts/install-pi.sh`),
falling back to Noto Sans Display. That fallback is a downgrade, not an
equivalent: Noto Sans Display ships only Regular and Bold, so every weight
between 500 and 700 collapses to bold and the whole UI reads heavy. If the panel
suddenly looks blunt, check that Inter is actually installed before touching any
CSS. Weight scale: 700 only at display sizes (clock, temperature), 600 for
headings and mid-size labels, 500 for small secondary text, 400 for body.
Headings need an explicit `font-weight` — they are `<h1>`/`<h2>`, and the browser
default is bold.

## Common commands

```bash
# Run the production app (loads .env, uses config/default.yaml, port 8080)
python -m smart_display.app

# Run the local demo server (config/local-demo.yaml, mocks, port 8090, separate data dir)
python -m smart_display.local_server         # or: smart-display-local

# Pull the whole Lightroom album onto the Pi now, not one photo per 30-min tick
# (stops kiosk + backend for ~1 min; --dry-run only reports new/edited/gone)
bash scripts/sync-album.sh [--dry-run]

# Demo mode without real accounts on the production entrypoint
APP_DEMO_MODE=true python -m smart_display.app

# Pre-merge checks (from AGENTS.md)
python3 -m compileall smart_display tests
python3 -m unittest discover -s tests

# Run a single test
python3 -m unittest tests.test_calendar_formatting
python3 -m unittest tests.test_calendar_formatting.TestClass.test_method
```

There is no linter or formatter wired up, and no `dev` extras installed — `pyproject.toml`'s `[project.optional-dependencies].dev` is empty. Don't introduce one without asking.

When the diff touches `smart_display/web/static/css/`, `smart_display/web/templates/` or `smart_display/watch_faces.py`, **also validate visually at 1024x600** — not just logically. `scripts/screenshot-states.py OUT_DIR` (Playwright, demo server on :8090) renders every face and state; `scripts/take-screenshots.sh` only does the six README faces. Diffs outside those paths don't need it. Touch-gesture changes (`createGestureTracker` and its two users in `app.js`, the screen order, the weather controls or the warning overlay) run `scripts/check-gestures.py` (WebKit + Chromium, `--self-test`) before and after. Changes to the state poll, `formatStamp`, `renderStatusRail`, the empty/behind texts, warning visibility or the weather chart's time axis run `scripts/check-freshness.py` (same engines; `--self-test` serves the v0.11.4 `app.js` and requires every positive case to fail against it). All of this is a development step, **not** a `/ship` gate: the screenshot scripts drive headless Chromium, and even Playwright's WebKit is not the device's WPE build, so their output is never evidence about the panel. The pre-merge checks in `AGENTS.md` (`compileall` + `unittest`) are the only ship gates in this repo.

## Architecture

Single Flask process. The UI is server-rendered HTML + vanilla JS that polls `/api/state`. There is no SPA, no bundler, no client state framework. The UI has three screens in a fixed, non-wrapping order (`SCREEN_ORDER` in `app.js`: home dashboard → full Spotify controller → weather detail, #22) swapped client-side by a horizontal swipe — all live in the same document, so it is still one page load with no router. Only the Spotify screen adds a second poll (`/api/spotify/queue`, 30 s), and only while it is visible. The weather screen adds no request: it renders from `/api/state`, and only while visible.

**Data flow (one direction):**

```
Scheduler threads → Providers → StateStore (+ DiskCache) → /api/state → app.js poll → DOM
```

- `smart_display/app.py` — `create_app()` wires config, the `StateStore`, the `ImageCache`, all providers, and the `Scheduler`. Each provider is registered as a `ScheduledJob` with its own refresh interval. `serve_app()` runs Waitress and **refuses to start** on anything other than `127.0.0.1`, `::1`, or `localhost` — a misconfigured `APP_HOST` is a loud `RuntimeError`, never a silent coerce.
- `smart_display/scheduler.py` — one daemon thread per job, runs `task()` immediately then on a fixed interval. Exceptions are logged, never raised — a failing provider must not take down the loop. Jobs can belong to a `pause_group`; the screensaver overlay pauses the `spotify` group via `POST /api/screensaver/state` with a TTL watchdog (`DEFAULT_PAUSE_TTL_SECONDS`) so a crashed frontend can't strand the polling. The frontend refreshes the TTL every 5 min while the screensaver is visible.
- `smart_display/state_store.py` — the **only** writable state. Thread-safe (single `Lock`). Every mutation calls `_persist_locked()` which writes `data/last_good.json` via `DiskCache`. On startup, the store rehydrates from `last_good.json` so the UI never boots empty after a power cycle. The payload carries `DASHBOARD_SCHEMA_VERSION`: any breaking change to the `DashboardState` shape **must bump** this constant, otherwise the next boot silently quarantines the stale cache as `last_good.json.corrupt-<ts>` and starts empty.
- `smart_display/providers/` — `BaseProvider` defines the contract: each provider owns one section name, fetches from its source, and either calls `state_store.update_section(...)` on success or `state_store.mark_error(...)` on failure. Provider failures degrade their own tile to `stale`/`error` and **must not** affect other tiles. This is a load-bearing invariant — see `AGENTS.md` "Failure-Mode-Denken".
- Snapshot status (#19, comment above `ProviderSnapshot`): `disabled` = not set up (calm, never an error) · `empty` = never fetched ("wird geladen") · `ok` = last fetch succeeded, **with or without items** · `stale` = first failure after good data · `error` = repeated failure, or a failure without good data. `updated_at` is the last good fetch, `None` if there never was one — `mark_error` and the load of a v0.11 cache never invent one. Only `ok` (or a behind snapshot with an `updated_at`) may say "Keine Termine". Boot grace (`StateStore.mark_error`/`retry_wait`): until a section has had one good answer *in this process*, failures in the first `FIRST_FETCH_GRACE_SECONDS` (180 s) change only the message, and the job retries every `FIRST_FETCH_RETRY_SECONDS` (30 s, `ScheduledJob.retry_after`). Keyed on answers since start, not on the persisted status — the usual boot has a cache and comes up `ok`. A failure no retry heals passes `transient=False` and goes straight to `error`.
- Weather detail (#22): four more sections beside `weather`, each with its own snapshot, age and last good data — `weather_hourly_geosphere` (`ClaefProvider`, C-LAEF hourly), `weather_hourly_openmeteo` (written by `OpenMeteoProvider` from the same request as `weather`), `weather_nowcast` (`IncaProvider`, 15-min rain) and `weather_warnings` (`WarningsProvider`, warn API point query → municipality). The GeoSphere providers live in `providers/weather_geosphere.py`; HTTP errors, `Retry-After` and the 429/5xx backoff in `providers/_fetch.py` (`fetch_json`, `Backoff`). Symbol tables (WMO and GeoSphere `sy`, never mixed) in `weather_codes.py`, day/night from `sun.py` (NOAA approximation). A point outside the GeoSphere area (`400 … outside of dataset bounds`, warn API `404`) sets the section to `disabled`, not `error`. The sections are additive (no schema bump); a warnings error never touches `weather`. `_with_runtime_flags` replaces the two raw hourly series with one `weather_hourly` (`select_hourly` in `weather_detail.py`: C-LAEF, else the whole series from Open-Meteo — never hour by hour; `fallback: true` only when GeoSphere actually failed, not while it is `disabled` or has not answered yet) and gives each warning `visible_until` and its display `title` (`prepare_warnings`, names from `weather_codes.warning_title` — the frontend has no name table; end exclusive, missing/inverted end = last good fetch + `UNBOUNDED_WARNING_HOLD`), computed with the hour-truncated clock so the ETag stays stable within an hour. Expiry itself is client-side (`visibleWarnings`), so an ended warning disappears without a fetch. The detail jobs have no `pause_group`: warnings keep being fetched while the screensaver runs.
- `smart_display/cache/` — `DiskCache` is a tiny atomic JSON writer. `ImageCache` downloads screensaver images, normalizes them to 1024x600 with Pillow, writes a manifest, and falls back to bundled demo images when the manifest is empty (`demo_images_enabled`). User-owned photos in `data/screensaver-local/` join the rotation and are never touched by Lightroom reconciliation. They are normalised to 1024x600 like the Lightroom images (copies in `data/screensaver/local/`, keyed by name+size+mtime, reused across restarts) and served as `/media/screensaver-local/<hex>.jpg`; dotfiles are skipped. Decoding happens on the provider thread (`prepare_local_photos`, bounded by `LOCAL_PREPARE_TICK_SECONDS`; non-JPEGs above `NON_JPEG_MAX_PIXELS` are skipped because only JPEG has a reduced-scale decode); a request thread decodes at most `LOCAL_PREPARE_PER_REQUEST` new photo and never while another thread is decoding, and a replaced photo's old copy stays servable for `LOCAL_RETIRE_GRACE_SECONDS`. The rotation shuffles per round under a lock (Waitress is multi-threaded), so every photo shows once before any repeats; a photo that becomes ready mid-round is slotted into the remaining round instead of re-shuffling it.
- Lightroom edits (#15): a rendition URL does **not** change when a photo is edited — its `revisions/<id>` is the original's. The provider therefore passes a fingerprint of each asset's `payload.develop` block alongside the URL (`_develop_fingerprint`), stored per manifest entry as `source_version`. A changed fingerprint re-downloads the photo under a new filename (old copy keeps its slot until then, same per-tick budget as new photos); byte-identical bytes mean Lightroom has not re-rendered yet and are retried with backoff, then accepted after `UNCHANGED_RENDITION_MAX_ATTEMPTS`. Entries without `source_version` (pre-#15) adopt the current fingerprint without a download.
- `smart_display/web/routes.py` — small Flask blueprint. Read endpoints: `GET /` (renders `templates/index.html` with `initial_state`), `GET /api/state`, `GET /api/screensaver/next`, `GET /health`, `GET /media/screensaver/<filename>`. `/api/state` carries a content-hash `ETag` (`_encode_state`, over the payload *including* `_with_runtime_flags`) and answers a matching `If-None-Match` with an empty `304`, so an unchanged poll costs the WPE browser no JSON parse; `app.js` seeds the tag from `config.state_etag`. Spotify is the only backend-interactive surface: `POST /api/spotify/{toggle,next,previous,volume,seek,play}` plus `GET /api/spotify/{devices,sources,queue}` proxy through to `SpotifyProvider`. The `devices`/`sources` reads and the `play` (start-on-Connect-speaker) write back the picker overlay; `seek` and `queue` back the Spotify detail screen. Device/playlist/queue lists are fetched on-demand (never on the state poll loop) and lightly cached in the provider, so they are **not** part of `DashboardState`. `queue` is the one list that falls back to its last good result when Spotify errors — bounded by `_QUEUE_STALE_MAX_AGE_SECONDS`, because an unbounded fallback would show tracks that finished hours ago. Starting playback needs the `playlist-read-private` scope — re-run `scripts/spotify-auth.py` if the refresh token predates it. The hero-clock watch-face toggle is purely client-side (`localStorage`), so it does not hit the backend.
- `smart_display/watch_faces.py` — stdlib-only helpers for the six hero-clock variants (`flip` split-flap digital [default], `lcd` seven-segment, `pulse` minimal digital with seconds, `qlocktwo` German word-clock grid, `qlocktwo-ooe` Upper-Austrian dialect, `analog` SVG). Exposes `qlocktwo_active_cells()`, `qlocktwo_ooe_active_cells()`, `analog_hand_angles()`, and `lcd_segments_for()` for server-side initial render; the same logic is mirrored in `app.js` for subsequent ticks. If you change a QLOCKTWO grid, word coordinates, or the LCD segment map, update **both** sides or tests will drift from the UI.
- `smart_display/web/static/js/app.js` and `static/css/app.css` — the entire frontend. The CSS is written for exactly one canvas, 1024×600: no media queries, no `clamp()`, no `vw` — every size is one pixel value, once. Don't add a second size ladder. The frontend poll interval (`ui_config.poll_interval_seconds`, set by `_poll_interval_seconds` in `routes.py`) is **decoupled** from the backend Spotify refresh: hitting the in-process `/api/state` is free, so it polls fast (`[3, 4]s`) when Spotify is enabled to surface playback/device changes quickly, and falls back to `30s` when Spotify is disabled. The upstream Spotify refresh cadence is the separate `refresh_intervals.spotify_seconds`.
- `smart_display/local_server.py` — a thin wrapper that forces `config/local-demo.yaml`, layers `.env` then `.env.local`, and uses a separate `data/local-demo` directory so desktop testing can't pollute the real cache.

**Config layering** (`smart_display/config.py`, `load_config`):

1. `config/default.yaml` (JSON-compatible)
2. Optional alternate config (`SMART_DISPLAY_CONFIG` env or explicit path) — deep-merged
3. `.env` values
4. `os.environ` — last write wins

Env-var → config-key mapping lives in `_apply_env_overrides`. `CALENDAR_NAME` is comma-split into a list. To add a new config key, extend both the dataclass in `config.py` and the env mapping.

## Conventions worth knowing

- Provider error path: never raise out of `refresh()`. Use `self.snapshot(status=..., error_message=...)` and `state_store.mark_error(...)`. A provider that has been `ok` and now fails should go to `stale`, not `error` — the store does this transition automatically the first time. The one exception is `transient=False` (a failure no retry heals), which goes straight to `error`. A source without configuration reports `self.snapshot(status="disabled")`, never `empty` and never `mark_error`.
- The state shape returned by `/api/state` is what `app.js` consumes; changing field names there is a frontend break. `DashboardState.to_dict()` / `from_dict()` are the source of truth and must round-trip cleanly because `last_good.json` is loaded back through `from_dict`.
- Spotify control endpoints return `{"ok": bool, "message": ...}` so the frontend can show transient errors without re-rendering the whole tile.
- There are no status badges and no card headings on the home screen by design — don't reintroduce them. Staleness is the age of the snapshot's `updated_at`, only for `stale`/`error`, formatted by `formatStamp` in `TIMEZONE` calendar days: "15:10" · "gestern 15:10" · "12. Okt. 15:10" · "30. Dez. 2025 15:10" ("" for a missing/invalid stamp). Each source reports where it lives (#20): weather "Stand …" in its line (the place name gives up its room), the calendar "Termine · …" in the status rail (no "Stand" — beside a timer chip the note has ~180 px; `compact` lets the year replace the time), Spotify "Spotify nicht erreichbar / zuletzt …" in the music bar. Never a device-wide "Offline". The photo screensaver omits behind weather. The age flips at midnight through `scheduleMidnightRerender`, no extra timer — midnight in `TIMEZONE`, not the browser's zone, re-armed at most hourly and rendering only when the date changed, so DST days cannot shift it. See `AGENTS.md` "Aktueller UI-Maßstab".
- Status rail precedence (#20): the timer chip always shows and never shrinks; beside it exactly one note, "Anzeige wird aktualisiert" (local poll failing, #18) before the calendar age. The note ellipsizes, the rail has `min-width: 0` so it can.
- State poll watchdog (#18, constants at the top of `app.js`): one request at a time (`fetchState` shares it with `exitScreensaver`), aborted after `STATE_REQUEST_TIMEOUT_MS` (10 s); the poll interval stays `POLL_INTERVAL_MS` (≥ 5 s). Success = 304 or a 200 whose payload has `weather`/`calendar`/`spotify`/`system` objects; HTTP errors, network errors, the timeout and garbage are failures. The rail hint shows once no poll has succeeded for `LOCAL_STALE_AFTER_MS` = 3 polls, at least 30 s (30 s on the Pi), and clears on the next success; provider errors keep rendering as usual. Measured with `performance.now()`, so NTP steps don't count; the screensaver pause restarts the window on wake unless the hint was already up. While the screensaver runs the poll drops to `SCREENSAVER_POLL_MS` (5 min, #22 — so warnings reach the screensaver line) with `quiet: true`: those polls never feed the lost-link hint.
- The weather screen (#22) is reached only by swiping from Spotify; the home screen gets no entry point. `renderWeatherDetail` runs only while it is visible and only when `weatherDetailKey` (sections, selection, hour — quarter hour in rain mode — and date) changes. The warning overlay `#warn-sheet` is a sibling of the stage like the picker; it closes when the screen is left or the screensaver starts, and the screensaver resets the selection (`resetWeatherView`). The screensaver warning line `#screensaver-warning` sits outside `.screensaver__info`, so the clock's wake area does not grow.
- The home and Spotify screens have **disjoint** control surfaces: the home screen is display-only, and every Spotify interaction — transport, volume, seek, device/playlist picker — lives on screen 2. The home music bar is a `<button>` whose only job is navigating there, so `renderSpotify` writes the home nodes for text/artwork, picks the bar's shape (`data-music` = `playing` / `idle` / `error` / `hidden` on `#cards-column`) and the detail nodes for everything else. Don't re-add a control to the home bar without also re-adding its node to `nodes`; most accesses there are unguarded and a missing id throws on the first poll (`tests/test_home_template.py` catches the reverse case).
- The agenda's "next event" is decided client-side (`classifyCalendar`): first item that is neither past nor all-day. Its relative label ("jetzt", "in N Min" up to 60) is refreshed by the minute tick as a text write; a full `renderCalendar` only runs when the phase (next item, past set) changes. The highlighted row costs one extra budget row — `section_extra_rows` in `compute_row_budget`, mirrored in JS.
- When adding a new scheduled job, register it in `create_app()` *and* add a refresh interval in `RefreshIntervalsConfig` + `default.yaml` + the env mapping. Don't hardcode an interval at the call site.
- `StateStore._persist_locked` is **debounced**: a payload that differs from the last write only in `VOLATILE_KEYS` (`progress_ms`, `generated_at`, `updated_at`) is written at most every `PERSIST_MIN_INTERVAL_SECONDS`; everything else writes immediately. `flush()` runs from `atexit`, and `serve_app` turns SIGTERM into `sys.exit` so atexit actually fires. If you add a field that changes on every refresh without meaning anything, put it in `VOLATILE_KEYS` or the SD write rate goes back to one per poll.
- `smart-display.service` is `Type=notify` + `WatchdogSec`. `smart_display/sd_notify.py` sends `READY=1` after `create_server` has bound, and `WATCHDOG=1` only after a loopback `GET /health` succeeds — so a wedged Waitress is restarted, not kept alive. Outside systemd every call is a no-op.
- Timers (`smart_display/timers.py`) derive `running/ringing/done` from the clock; nothing but `ends_at` and `dismissed` is stored. `DashboardState.timers` was added with a default, so no schema bump — keep additive changes additive.
- `POST /api/system/{shutdown,reboot}` is gated twice: `app.allow_shutdown` (false everywhere except the Pi's `.env`) and a fixed argv through `sudo -n` matching `deploy/sudoers/smart-display`. Never build the command from request data.
- Night window: `smart_display/night.py` is the source of truth, `app.js` mirrors `parseHhmm`/`isNightMinutes`. The server renders `data-night` for the first frame; the minute tick re-evaluates client-side.
- `system.time_synced` and `timers` are injected at request time by `_with_runtime_flags` in `routes.py`; they are not part of what `StateStore` persists.

## Post-ship deploy (runs as part of `/ship`)

After a successful push, deploy to the Pi:

```bash
bash scripts/deploy-pi.sh
```

**Immer** — bedingungslos, kein Ermessen anhand des Diffs, keine Rückfrage. Nicht
überspringen, weil der Diff „nach Doku aussieht", und nicht gegen das 24/7-Panel
abwägen: diese Entscheidung trifft `deploy-pi.sh` selbst und trifft sie besser.
Es synct jedes Mal (Dateien schreiben ist für die laufenden Units unsichtbar) und
startet die beiden systemd-Units **nur**, wenn rsync tatsächlich etwas übertragen
hat. `docs/`, `tests/`, `.github/` und alle `*.md` sind aus dem rsync
ausgeschlossen — ein reiner Doku-Ship überträgt null Dateien, druckt
`Neustart übersprungen`, und das Panel bleibt an. Verifiziert gegen das Gerät:
eine geänderte `README.md` und sechs geänderte `docs/screenshots/*.png` erzeugen
zusammen keine einzige rsync-Zeile.

Diese Logik lebt im Skript. Hier nicht nachbauen, und keine Bedingung zurück in
diesen Abschnitt schreiben — genau das war der alte Zustand, und er hing daran,
dass ein Modell jedes Mal richtig rät. Bewusst übersteuern nur mit
`--force-restart`, wenn das Gerät nachweislich driftet (etwa nach einem
abgebrochenen Deploy).

Das Gate ist absichtlich grob: rsync vergleicht Größe und mtime, keine Prüfsumme.
Eine inhaltlich identische Datei mit neuerer mtime (Branch-Wechsel, `stash pop`)
löst einen überflüssigen Neustart aus. Richtige Fehlerrichtung — lieber einmal zu
viel neu starten als Code ausliefern und den Restart verschlucken.

Exit-Codes: `0` = erledigt · `3` = Pi nicht erreichbar, es wurde nichts deployed.
Bei `3` ist der Ship weder rot noch grün: der Push ist raus und wird **nicht**
rückgängig gemacht, das Gerät läuft auf altem Stand. Genau so melden und auf
`bash scripts/deploy-pi.sh` als Nachhol-Kommando verweisen. Jeder andere Exit ≠ 0
ist ein **fehlgeschlagener Deploy** — den fehlgeschlagenen Schritt benennen, den
Ship nicht als grün darstellen.

`deploy-pi.sh` rsyncs the working tree to `/opt/smart-display` and restarts the
two units **iff that rsync transferred something** (see above). It is *not*
`install-pi.sh` — that one provisions a fresh machine (apt, venv, systemd units)
and is the wrong tool for a code change. Target host comes from `PI_HOST`
(default `pi@192.168.178.22`).

Drei Dinge, die `deploy-pi.sh` bewusst **nicht** kann, und die deshalb
`install-pi.sh` brauchen:

- **Drop-ins, sudoers, config.txt, cmdline.txt.** Schritt 8 des Installers legt
  `/etc/systemd/journald.conf.d/`, `/etc/systemd/system.conf.d/`,
  `/etc/sudoers.d/smart-display`, `dtparam=watchdog=on`, optional
  `dtoverlay=gpio-shutdown` und `video=HDMI-A-1:1024x600@60D` in `cmdline.txt` an.
  Nichts davon erreicht der rsync. Der `video=`-Eintrag (#16) erzwingt HDMI als
  verbunden: Unter vollem KMS liest der Kernel die Hotplug-Leitung selbst,
  `hdmi_force_hotplug` in `config.txt` wirkt dort nicht. Fällt die Leitung
  kurz, meldet DRM sonst `disconnected` und cog scheitert bei jedem Start mit
  „Failed to initialize DRM“, bis sie zurückkommt.

- **Geänderte systemd-Units.** `install-pi.sh` *kopiert* `deploy/systemd/*.service`
  nach `/etc/systemd/system/` (kein Symlink). Der rsync erreicht nur
  `/opt/smart-display/deploy/systemd/`, also wird eine Unit-Änderung zwar
  übertragen und löst einen Restart aus, **wirkt aber nicht**. Das
  `daemon-reload` im Skript ist reine Hygiene und ändert daran nichts.
- **Neue Dependencies.** Es läuft kein `pip install -e .`. Ein geändertes
  `pyproject.toml` landet als Datei auf dem Gerät, das venv kennt das Paket
  nicht → Backend startet nicht → der Health-Loop läuft 30 s leer und das Skript
  bricht ab.

The rsync excludes two disjoint sets, and the distinction matters:

- `RSYNC_OWNED` — what the device owns and the repo cannot recreate: `.env`,
  `.kiosk.env`, `data/` (state cache + screensaver images), `.venv`, the
  editable-install egg-info — plus local build/cache dirt (`.DS_Store`,
  `.pytest_cache`, …), because rsync does not read `.gitignore` and a
  Finder-written `.DS_Store` would otherwise trip the restart gate on every
  ship. `install-pi.sh` carries a **deliberately shorter** list (it lays down a
  full checkout, `.git` included), so the two are not meant to match line for
  line — only the device-owned entries must never be dropped here.
- `RSYNC_SKIP` — `docs/`, `tests/`, `.github/`, `*.md`: never read at runtime.
  Excluding them is what makes the restart gate work. **Nicht** nach
  `install-pi.sh` spiegeln (der Installer legt den Vollcheckout an). Falls hier je
  etwas dazukommt, das doch aufs Gerät muss, gehört es aus der Liste raus — keine
  Ausnahme einbauen.

Nebeneffekt: `docs/`, `tests/` und `*.md`, die aus früheren Deploys auf dem Pi
liegen, werden von `--delete` **nicht** entfernt (rsync schützt ausgeschlossene
Pfade). Sie bleiben als tote Kopien liegen — folgenlos; bei Bedarf einmal
`ssh $PI_HOST 'sudo rm -rf /opt/smart-display/{docs,tests}'`.

The script reports version, both unit states, the kiosk restart count and
`/health`. It **cannot** verify the panel visually — Cog renders straight to DRM,
so there is no screenshot path on the device. A green report means the services
came back, not that the UI is correct; say so rather than implying it was seen.

## Deployment shape

The Pi runs two systemd units from `deploy/systemd/`: `smart-display.service` (the Waitress backend) and `smart-display-kiosk.service`. Anything that breaks unattended startup, idle behavior, or the screensaver fallback is a release blocker.

The kiosk unit is `Requires=` + `After=` the backend, so a backend stop or restart carries down to it, and `WantedBy=smart-display.service` (#17) so a backend *start* brings it back too — without that, `systemctl stop smart-display` + `start` left the panel dark. Any backend start counts, the automatic one after a crash included, so a kiosk stopped by hand comes back then as well. Like every unit change this needs `install-pi.sh` (its `systemctl enable` creates the `smart-display.service.wants/` link); `deploy-pi.sh` cannot activate it. Live on the Pi since 2026-10-25. `scripts/pi/check-unit-restarts.sh` (on the Pi, `sudo`) checks restart, stop+start and a crash; run it after any unit change.

The kiosk unit always execs `deploy/kiosk/start-kiosk.sh`, which dispatches on `KIOSK_BROWSER` from `/opt/smart-display/.kiosk.env` — **there are two mutually exclusive kiosk paths**:

- `chromium` (the script's default) — Chromium in `--kiosk --app=...`, started through `xinit` with `deploy/x11/kiosk-session.sh` as the X session. Engine: Blink.
- `cog` — Cog/WPE rendering straight to DRM/KMS, no X server at all. Engine: WebKit. `deploy/x11/kiosk-session.sh` is never executed on this path.

**The live Pi runs `cog`** (`KIOSK_BROWSER=cog`), which `README.md` recommends for the Zero 2 W. Two consequences worth knowing before you touch either: edits to `deploy/x11/kiosk-session.sh` — Chromium flags included — have no effect on the current device, and browser-behaviour questions there must be judged against **WebKit**, not Chromium. Note that `scripts/take-screenshots.sh` and any local visual check run headless *Chrome*, so that validation does not cover the engine the device actually uses.
