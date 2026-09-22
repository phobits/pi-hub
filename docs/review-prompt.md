# Umfassender Repo-Review – Prompt

Verwende diesen Prompt, um eine gründliche, hardware-bewusste Review des
Smart-Display-Repos (`phobits/pi-hub`) durchzuführen. Die Review soll ehrlich,
priorisiert und reproduzierbar sein – keine generische Checkliste, sondern
konkrete Fundstellen mit `datei:zeile`.

---

## Rolle & Rahmen

Du bist ein Senior-Reviewer mit Erfahrung in Embedded-Python, Flask, Systemd
und Kiosk-Deployments. Das Ziel-Gerät ist ein **Raspberry Pi Zero 2 W
(512 MB RAM, ARM, Dauerbetrieb, 1024×600-Touch, keine Tastatur)** im Chromium-
Kiosk-Modus. Lies `CLAUDE.md` und `AGENTS.md`, bevor du urteilst – die dort
festgelegte UI-Hierarchie und Failure-Mode-Regeln sind bewusst so gewählt und
teils mehrfach revidiert worden.

## Nicht-Ziele

- Keine neuen Frameworks, Bundler, Client-State-Libraries vorschlagen.
- Keine Umstellung auf async/FastAPI/SPAs „weil moderner".
- Keine Lint-/Format-Tools einführen, ohne den RAM-/Wartungs-Trade-off zu begründen.
- Keine kosmetischen Refactors, die kein konkretes Problem lösen.

## Methodik

1. **Verstehen vor Bewerten.** Gehe Datenfluss `Scheduler → Provider → StateStore → /api/state → app.js → DOM` einmal durch, bevor du Befunde formulierst.
2. **Befunde mit Beleg.** Jeder Punkt referenziert `pfad/datei.py:zeilennr` und zitiert die relevante Zeile.
3. **Priorisiere nach Impact auf den Pi.** Ein RAM-Leck oder ein Scheduler-Thread, der stillschweigend stirbt, ist wichtiger als ein stilistischer Makel.
4. **Trenne Fakt von Meinung.** Markiere Vorschläge als `[Bug]`, `[Risiko]`, `[Perf]`, `[Wartbarkeit]`, `[Nit]`.
5. **Schlage Fixes nur vor, wenn du sie begründen kannst.** Bei Unsicherheit: Frage statt raten.

## Review-Dimensionen

### 1. Hardware- & Laufzeit-Constraints
- RAM-Budget: Gibt es unbegrenzt wachsende Caches, Log-Buffer, in-memory Listen? (z. B. `state_store`, `image_cache`, Scheduler-Historie)
- Dauerbetrieb: Thread-/Socket-/Datei-Leaks, hängende HTTP-Sessions, nicht geschlossene Pillow-Images.
- ARM-/Pi-Zero-Besonderheiten: Pillow-Operationen, große JSON-Serialisierungen, synchrone Netz-Calls im Scheduler-Tick.
- Idle-/Nachtverhalten: Screensaver-TTL (`DEFAULT_PAUSE_TTL_SECONDS`), `spotify`-Pause-Group, Watchdog-Reset.

### 2. Architektur-Invarianten (aus `CLAUDE.md`)
- Ist der `StateStore` wirklich die einzige schreibbare Stelle? Umgehen Provider den Lock?
- Werden Provider-Fehler isoliert? Kann ein defekter Provider einen anderen Tile zum Kippen bringen?
- Round-trippt `DashboardState.to_dict/from_dict` sauber? Ist die `DASHBOARD_SCHEMA_VERSION` bei Shape-Änderungen gebumpt?
- Hält `serve_app()` die Bind-Adress-Restriktion (`127.0.0.1`, `::1`, `localhost`) – laut, nicht still?

### 3. Scheduler & Fehlerpfade
- Daemon-Threads: Exceptions geloggt und geschluckt, Loop überlebt?
- Intervall-Konfiguration konsistent in `RefreshIntervalsConfig` + `default.yaml` + Env-Mapping?
- Kein hardcodiertes Intervall am Call-Site?
- `ok` → `stale` → `error`-Übergang korrekt (erster Fehler nach `ok` = `stale`)?

### 4. Web-Layer
- `/api/state`-Shape stabil gegenüber `app.js`? Feldnamen-Drift?
- Spotify-Proxy-Endpunkte: Input-Validierung (Volume-Range, Payload-Typen), konsistente `{ok, message}`-Antworten, kein Token-Leak in Logs/Responses.
- `GET /media/screensaver/<filename>`: Path-Traversal-Schutz?
- CSRF/Trust-Modell: Reicht das Bind-Lockdown, oder gibt es Surfaces, die trotzdem härter sein sollten?

### 5. Frontend (`app.js`, `app.css`, `index.html`)
- Polling-Intervall sauber aus `ui_config` (`_poll_interval_seconds` in `routes.py`: `[3,4]s` bei aktivem Spotify, sonst `30s`)? Es ist bewusst vom Spotify-Upstream-Refresh (`refresh_intervals.spotify_seconds`) entkoppelt.
- Läuft periodische Arbeit nur für den *sichtbaren* Screen (Fortschrittsbalken, analoger Sekundenzeiger, Queue-Poll)? Der versteckte Screen bleibt im DOM (`visibility: hidden`), seine Timer dürfen den Pi nicht wach halten.
- DOM-Updates minimal-invasiv, keine Full-Re-Renders, die den Pi-GPU-Composite belasten?
- LocalStorage-Keys dokumentiert (Hero-Clock-Watch-Face-Toggle)?
- Watch-Face-Logik (flip/lcd/pulse/qlocktwo/qlocktwo-ooe/analog) deckungsgleich zwischen `watch_faces.py` und `app.js`? (QLOCKTWO-Grid, LCD-Segmente, Uhrzeiger-Winkel)
- Umlaute: echte Zeichen (`ä`, `ö`, `ü`, `ß`), keine HTML-Entities oder Transliteration.

### 6. UI-Hierarchie (Re-Litigation vermeiden)
- Keine Wiedereinführung von: Status-Badges im `ok`-Zustand, „Startbildschirm"-Heading, separatem Wetter-Tile rechts, Timezone-Label unter dem Hero-Datum.
- 1024×600 tatsächlich validiert, nicht nur Desktop-Viewport.
- Zwei Screens (Homescreen + Spotify-Steuerung, horizontaler Wisch, keine Seitenindikatoren): Beide liegen im selben Dokument — kein Router, kein zweiter Seitenaufruf, genau ein `<main>`. Der Screensaver setzt immer auf den Homescreen zurück.
- Layout-Kopplungen: Ändert eine Sektion die Höhe einer anderen (z. B. `is-spotify-active` auf die Kalenderzeile), muss die betroffene Sektion neu vermessen werden — die Render-Dedup pro Sektion darf das nicht überspringen.

### 7. Persistenz & Cache
- `DiskCache`: Atomic-Write-Pfad (tmp + rename), Fehlerpfade, Quarantäne bei Schema-Mismatch.
- `ImageCache`: Normalisierung auf 1024×600, Pillow-Ressourcen-Handling, Fallback auf Demo-Bilder wenn Manifest leer (`demo_images_enabled`).
- Korrupte `last_good.json`: Wird sauber quarantäniert (`*.corrupt-<ts>`)?

### 8. Config & Env
- Layer-Reihenfolge korrekt (`default.yaml` → alt → `.env` → `os.environ`)?
- `CALENDAR_NAME` wirklich Komma-gesplittet?
- `APP_DEMO_MODE`, `SMART_DISPLAY_CONFIG` konsistent behandelt?
- Neue Keys vollständig: Dataclass + Env-Mapping + YAML-Default.

### 9. Deployment (`deploy/systemd`, `deploy/x11`)
- Units idempotent, Restart-Policy sinnvoll, keine harten Abhängigkeiten auf Netz beim Boot.
- Kiosk-Session: Chromium-Flags (`--kiosk --app=…`), GPU-/Cache-Tuning für 512 MB?
- Log-Rotation / Journald-Größe begrenzt?
- Bricht irgendetwas den unbeaufsichtigten Startup, Idle oder Screensaver-Fallback? → Release-Blocker.

### 10. Tests
- `python3 -m compileall` & `python3 -m unittest discover` laufen grün?
- Decken Tests die kritischen Invarianten ab: Provider-Isolation, Schema-Round-Trip, Watch-Face-Parität, Config-Override-Reihenfolge?
- Flaky Tests? Netz-abhängige Tests ohne Mock?

### 11. Sicherheit
- Secrets nicht im Repo, keine Tokens in Logs/Exceptions.
- Spotify-OAuth-Refresh-Flow: Token-Lifecycle, Fehler-Handling, kein Infinite-Retry-Sturm.
- Dependency-Flächen: `pyproject.toml`-Pins, bekannte CVE-anfällige Versionen?

### 12. Dokumentations-Kohärenz
- Stimmt `README.md` mit `CLAUDE.md`/`AGENTS.md` überein?
- Sind die sechs Watch-Faces, die Demo-Modi, die Config-Keys aktuell dokumentiert?
- Screenshots in `docs/screenshots` aktuell?

## Output-Format

Liefere **ein** Markdown-Dokument mit dieser Struktur:

```
# Review <commit-sha>

## TL;DR
3–5 Bullets: was blockiert Release, was ist die größte Baustelle, was ist solide.

## Release-Blocker
(nur harte Bugs / Invariant-Verletzungen / Startup-Regressionen)
- [Bug] <datei:zeile> – <beschreibung> – <vorgeschlagener fix>

## Risiken (hoch → niedrig)
- [Risiko] …
- [Perf] …

## Wartbarkeit
- [Wartbarkeit] …

## Nits
(gesammelt, einzeilig)

## Positives
Was explizit gut gelöst ist – damit es so bleibt.

## Offene Fragen
Dinge, die du ohne Rücksprache nicht entscheiden kannst.
```

## Selbstkontrolle vor Abgabe

- [ ] Jeder Befund hat `datei:zeile` + Zitat.
- [ ] Keine Empfehlung widerspricht `CLAUDE.md` / `AGENTS.md` ohne explizite Begründung.
- [ ] Kein Vorschlag bricht die 512-MB-/ARM-/Kiosk-Constraints.
- [ ] Keine Re-Litigation bereits entschiedener UI-Fragen.
- [ ] Tests wurden tatsächlich ausgeführt, nicht nur gelesen.
- [ ] Bei Unsicherheit: offene Frage statt spekulativer Fix.
