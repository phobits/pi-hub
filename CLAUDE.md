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

When the diff touches `smart_display/web/static/css/`, `smart_display/web/templates/` or `smart_display/watch_faces.py`, **also validate visually at 1024x600** — not just logically. Diffs outside those paths don't need it. This is a development step, **not** a `/ship` gate: `scripts/take-screenshots.sh` drives headless Chrome while the device runs WebKit, so its output is never evidence about the panel. The pre-merge checks in `AGENTS.md` (`compileall` + `unittest`) are the only ship gates in this repo.

## Architecture

Single Flask process. The UI is server-rendered HTML + vanilla JS that polls `/api/state`. There is no SPA, no bundler, no client state framework. The UI has two screens (home dashboard, full Spotify controller) swapped client-side by a horizontal swipe — both live in the same document, so it is still one page load with no router. Only the Spotify screen adds a second poll (`/api/spotify/queue`, 30 s), and only while it is visible.

**Data flow (one direction):**

```
Scheduler threads → Providers → StateStore (+ DiskCache) → /api/state → app.js poll → DOM
```

- `smart_display/app.py` — `create_app()` wires config, the `StateStore`, the `ImageCache`, all providers, and the `Scheduler`. Each provider is registered as a `ScheduledJob` with its own refresh interval. `serve_app()` runs Waitress and **refuses to start** on anything other than `127.0.0.1`, `::1`, or `localhost` — a misconfigured `APP_HOST` is a loud `RuntimeError`, never a silent coerce.
- `smart_display/scheduler.py` — one daemon thread per job, runs `task()` immediately then on a fixed interval. Exceptions are logged, never raised — a failing provider must not take down the loop. Jobs can belong to a `pause_group`; the screensaver overlay pauses the `spotify` group via `POST /api/screensaver/state` with a TTL watchdog (`DEFAULT_PAUSE_TTL_SECONDS`) so a crashed frontend can't strand the polling. The frontend refreshes the TTL every 5 min while the screensaver is visible.
- `smart_display/state_store.py` — the **only** writable state. Thread-safe (single `Lock`). Every mutation calls `_persist_locked()` which writes `data/last_good.json` via `DiskCache`. On startup, the store rehydrates from `last_good.json` so the UI never boots empty after a power cycle. The payload carries `DASHBOARD_SCHEMA_VERSION`: any breaking change to the `DashboardState` shape **must bump** this constant, otherwise the next boot silently quarantines the stale cache as `last_good.json.corrupt-<ts>` and starts empty.
- `smart_display/providers/` — `BaseProvider` defines the contract: each provider owns one section name, fetches from its source, and either calls `state_store.update_section(...)` on success or `state_store.mark_error(...)` on failure. Provider failures degrade their own tile to `stale`/`error` and **must not** affect other tiles. This is a load-bearing invariant — see `AGENTS.md` "Failure-Mode-Denken".
- `smart_display/cache/` — `DiskCache` is a tiny atomic JSON writer. `ImageCache` downloads screensaver images, normalizes them to 1024x600 with Pillow, writes a manifest, and falls back to bundled demo images when the manifest is empty (`demo_images_enabled`).
- `smart_display/web/routes.py` — small Flask blueprint. Read endpoints: `GET /` (renders `templates/index.html` with `initial_state`), `GET /api/state`, `GET /api/screensaver/next`, `GET /health`, `GET /media/screensaver/<filename>`. Spotify is the only backend-interactive surface: `POST /api/spotify/{toggle,next,previous,volume,seek,play}` plus `GET /api/spotify/{devices,sources,queue}` proxy through to `SpotifyProvider`. The `devices`/`sources` reads and the `play` (start-on-Connect-speaker) write back the picker overlay; `seek` and `queue` back the Spotify detail screen. Device/playlist/queue lists are fetched on-demand (never on the state poll loop) and lightly cached in the provider, so they are **not** part of `DashboardState`. `queue` is the one list that falls back to its last good result when Spotify errors — bounded by `_QUEUE_STALE_MAX_AGE_SECONDS`, because an unbounded fallback would show tracks that finished hours ago. Starting playback needs the `playlist-read-private` scope — re-run `scripts/spotify-auth.py` if the refresh token predates it. The hero-clock watch-face toggle is purely client-side (`localStorage`), so it does not hit the backend.
- `smart_display/watch_faces.py` — stdlib-only helpers for the six hero-clock variants (`flip` split-flap digital [default], `lcd` seven-segment, `pulse` minimal digital with seconds, `qlocktwo` German word-clock grid, `qlocktwo-ooe` Upper-Austrian dialect, `analog` SVG). Exposes `qlocktwo_active_cells()`, `qlocktwo_ooe_active_cells()`, `analog_hand_angles()`, and `lcd_segments_for()` for server-side initial render; the same logic is mirrored in `app.js` for subsequent ticks. If you change a QLOCKTWO grid, word coordinates, or the LCD segment map, update **both** sides or tests will drift from the UI.
- `smart_display/web/static/js/app.js` and `static/css/app.css` — the entire frontend. The frontend poll interval (`ui_config.poll_interval_seconds`, set by `_poll_interval_seconds` in `routes.py`) is **decoupled** from the backend Spotify refresh: hitting the in-process `/api/state` is free, so it polls fast (`[3, 4]s`) when Spotify is enabled to surface playback/device changes quickly, and falls back to `30s` when Spotify is disabled. The upstream Spotify refresh cadence is the separate `refresh_intervals.spotify_seconds`.
- `smart_display/local_server.py` — a thin wrapper that forces `config/local-demo.yaml`, layers `.env` then `.env.local`, and uses a separate `data/local-demo` directory so desktop testing can't pollute the real cache.

**Config layering** (`smart_display/config.py`, `load_config`):

1. `config/default.yaml` (JSON-compatible)
2. Optional alternate config (`SMART_DISPLAY_CONFIG` env or explicit path) — deep-merged
3. `.env` values
4. `os.environ` — last write wins

Env-var → config-key mapping lives in `_apply_env_overrides`. `CALENDAR_NAME` is comma-split into a list. To add a new config key, extend both the dataclass in `config.py` and the env mapping.

## Conventions worth knowing

- Provider error path: never raise out of `refresh()`. Use `self.snapshot(status=..., error_message=...)` and `state_store.mark_error(...)`. A provider that has been `ok` and now fails should go to `stale`, not `error` — the store does this transition automatically the first time.
- The state shape returned by `/api/state` is what `app.js` consumes; changing field names there is a frontend break. `DashboardState.to_dict()` / `from_dict()` are the source of truth and must round-trip cleanly because `last_good.json` is loaded back through `from_dict`.
- Spotify control endpoints return `{"ok": bool, "message": ...}` so the frontend can show transient errors without re-rendering the whole tile.
- Status badges are hidden in the normal `ok` state by design — don't reintroduce them. Same for the removed "Startbildschirm" heading, the standalone weather card on the right, the decorative timezone label that used to sit under the hero date, and the home screen's Spotify status pill (all dropped to free space on 1024×600). See `AGENTS.md` "Aktueller UI-Maßstab".
- The two screens have **disjoint** control surfaces: the home screen is display-only, and every Spotify interaction — transport, volume, seek, device/playlist picker — lives on screen 2. The home Spotify tile is a `<button>` whose only job is navigating there, so `renderSpotify` writes the home nodes for text/artwork and the detail nodes for everything else. Don't re-add a control to the home tile without also re-adding its node to `nodes`; most accesses there are unguarded and a missing id throws on the first poll.
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

- **Drop-ins, sudoers, config.txt.** Schritt 8 des Installers legt
  `/etc/systemd/journald.conf.d/`, `/etc/systemd/system.conf.d/`,
  `/etc/sudoers.d/smart-display`, `dtparam=watchdog=on` und optional
  `dtoverlay=gpio-shutdown` an. Nichts davon erreicht der rsync.

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

The kiosk unit always execs `deploy/kiosk/start-kiosk.sh`, which dispatches on `KIOSK_BROWSER` from `/opt/smart-display/.kiosk.env` — **there are two mutually exclusive kiosk paths**:

- `chromium` (the script's default) — Chromium in `--kiosk --app=...`, started through `xinit` with `deploy/x11/kiosk-session.sh` as the X session. Engine: Blink.
- `cog` — Cog/WPE rendering straight to DRM/KMS, no X server at all. Engine: WebKit. `deploy/x11/kiosk-session.sh` is never executed on this path.

**The live Pi runs `cog`** (`KIOSK_BROWSER=cog`), which `README.md` recommends for the Zero 2 W. Two consequences worth knowing before you touch either: edits to `deploy/x11/kiosk-session.sh` — Chromium flags included — have no effect on the current device, and browser-behaviour questions there must be judged against **WebKit**, not Chromium. Note that `scripts/take-screenshots.sh` and any local visual check run headless *Chrome*, so that validation does not cover the engine the device actually uses.
