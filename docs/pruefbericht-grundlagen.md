# Prüfbericht Grundlagen-Auftrag (G-01 bis G-04)

Stand 2026-10-25. Gilt für `docs/dev-auftrag-grundlagen.md`, umgesetzt in
v0.12.0 (`66fb56d`), v0.12.1 (`ccbebc7`) und v0.12.2 als #18 (G-01), #19 (G-02) und
#20 (G-03). Alle drei Versionen sind auf dem Pi deployed.

**Teilabschluss:** G-01 bis G-03 sind umgesetzt und lokal belegt. **G-04 ist
offen**: Die Geräteabnahme und der 72-Stunden-Lauf haben nicht stattgefunden.
Was am Gerät gemessen wurde, steht unter „Gerät“. Die Screenshots unten stammen
aus headless Chromium mit Demo-Daten und sind **keine** Geräteabnahme.

## Änderungen

| G-ID | Was | Wo |
|---|---|---|
| G-01 | Immer nur eine State-Abfrage, Abbruch nach Timeout. Hinweis „Anzeige wird aktualisiert“ in der Statusleiste, wenn keine Abfrage mehr gelingt. Gemessen monoton, die Screensaver-Pause zählt nicht. | `app.js` (`requestState`, `noteStateContact`) |
| G-02 | Neuer Status `disabled`. `ok` heißt erfolgreich, auch ohne Einträge. Ein Fehler ohne je gute Daten wird `error`. Nach dem Start gilt eine Boot-Schonfrist mit Schnellversuchen. Dauerhafte Fehler (`transient=False`) zeigen sich sofort. Spotify: Ein Fehler ohne Body ist ein Fehler. | `state_store.py`, `scheduler.py`, `app.py`, Provider |
| G-03 | `formatStamp` zählt Kalendertage in `TIMEZONE`. Jede Quelle meldet ihr Alter an ihrer eigenen Stelle. Feste Vorrangregel für die Statusleiste. Der Screensaver lässt veraltetes Wetter weg. Mitternacht wird in `TIMEZONE` bestimmt. | `app.js`, `app.css`, `index.html` |

## Gewählte Schwellen

| Konstante | Wert | Begründung |
|---|---|---|
| `STATE_REQUEST_TIMEOUT_MS` | 10 s | Liegt deutlich über einer normalen Loopback-Antwort, auch auf dem Zero 2 W. Liegt unter dem 30-s-Fenster, damit ein hängender Request den Hinweis nicht blockiert. |
| `LOCAL_STALE_AFTER_MS` | max(3 Polls, 30 s); am Pi 30 s | Ein einzelner verpasster Poll erzeugt keinen Hinweis, eine echte Störung ist nach einer halben Minute sichtbar. |
| `FIRST_FETCH_GRACE_SECONDS` | 180 s | Deckt einen Pi ab, der vor seinem WLAN hochkommt. Danach erscheint der Fehler ehrlich. |
| `FIRST_FETCH_RETRY_SECONDS` | 30 s | In der Schonfrist höchstens 6 Versuche je Quelle. Spätestens nach etwa 3,5 min stehen frische Daten oder ein Fehler. |

## Ergebnisse

| Prüfung | Ergebnis |
|---|---|
| `python3 -m compileall smart_display tests` | ok |
| `python3 -m unittest discover -s tests` | 339 Tests ok. Die Provider-Zustandsmatrix in `tests/test_provider_states.py` umfasst 34 Fälle. |
| `scripts/check-freshness.py --self-test` (WebKit + Chromium) | 54/54 ok. v0.11.4 fällt in 42/42 positiven Fällen durch. |
| `scripts/screenshot-states.py` | 32 Zustände bei 1024×600 gesichtet. Dabei behoben: Eine lange Notiz in der Statusleiste schob den Timer-Chip aus der Spalte. |
| Code-Reviews (`/code-review medium`) | v0.12.0: 7 Findings, 5 behoben, 2 in v0.12.1. v0.12.1, erste Runde: 8 Findings, alle behoben. v0.12.1, zweite Runde: 8 Findings, 7 in v0.12.2 behoben. Liegen gelassen: CalDAV-Auth-Fehler bekommen die Schonfrist, sind aber nach spätestens 3,5 min sichtbar. Über v0.12.2 lief bewusst keine weitere Runde. |

G-01 deckt check-freshness mit diesen Fällen ab: Dienst weg, hängende Anfrage,
HTTP 500, ungültiges JSON und ungültige Payload, Wiederkehr ohne Reload,
wiederholte 304, 2 h Screensaver, NTP-Sprung vor und zurück.

G-02 deckt die Zustandsmatrix ab: deaktiviert, fehlende Konfiguration,
Erstabruf läuft, Schonfrist mit und ohne Cache, falsche Zugangsdaten, erfolgreich
leer, gefüllt, späterer Fehler mit Cache, Wiederkehr leer, Unabhängigkeit der
Abschnitte. Dazu kommen ein Neustart mit einem `last_good.json` aus v0.11,
dauerhafte Fehler über die echten Provider-Pfade und Spotify 204/3xx/503 ohne
Body oder mit `null`, abgelehnter Token-Endpoint gegen einen 400 ohne OAuth-Code.

G-03 deckt check-freshness mit `page.clock` ab: heute, gestern, 48 h, Vorjahr,
fehlender und ungültiger Zeitstempel, Mitternacht (auch mit Browser in UTC),
Jahreswechsel, Herbst- und Frühjahrsumstellung `Europe/Vienna`, Statusleiste
neben `1:02:05` ohne Überlappung.

## Reproduzieren

```bash
python3 -m compileall smart_display tests && python3 -m unittest discover -s tests
python3 -m smart_display.local_server &                  # Demo-Server :8090
python3 scripts/check-freshness.py --self-test           # Playwright nötig
python3 scripts/screenshot-states.py OUT_DIR
```

## Messartefakte

Die folgenden Bilder stammen aus `screenshot-states.py` mit Demo-Daten, ohne
Tokens und ohne private Termine. Den vollständigen Satz erzeugt der Befehl oben.

| Zustand | Bild |
|---|---|
| Erstabruf ohne gute Daten, alle Quellen | [home-first-fetch-failed.png](pruefbericht/home-first-fetch-failed.png) |
| Lokale Verbindungsstörung neben einem Timer | [home-local-lost-timer.png](pruefbericht/home-local-lost-timer.png) |
| Mehrtägig veraltete Daten, Uhrstil `flip` | [home-days-old-flip.png](pruefbericht/home-days-old-flip.png) |
| Daten aus dem Vorjahr neben einem Timer | [home-year-old-timer.png](pruefbericht/home-year-old-timer.png) |

## Gerät

Nach dem Deploy von v0.12.1 auf `pihub` (v0.12.2 siehe Ship-Ausgabe):

- Backend und Kiosk laufen (`active`), der Kiosk ohne Neustart.
- `/health` antwortet mit 200. Wetter, Kalender, Spotify und Screensaver melden
  `ok`, ohne Fehlermeldung.
- Die neue `app.js` wird ausgeliefert.
- Die Systemzeitzone ist `Europe/Vienna`.

Das Panel selbst ist nicht gesichtet. Cog rendert direkt auf DRM, es gibt keinen
Screenshot-Pfad.

## Offene Punkte

- **G-04:** Die Geräteabnahme nach dem Plan in `dev-auftrag-grundlagen.md` und
  der 72-Stunden-Lauf fehlen. Am Panel zu prüfen, nach Ziehen und Wiederherstellen
  des WLANs: In den ersten 3 min erscheint kein Fehler; nach Stopp des Backends
  steht nach etwa 30 s „Anzeige wird aktualisiert“; danach kommt das Panel ohne
  Reload zurück.
- **Kiosk-SEGV beim Stoppen:** Cog stürzt auf SIGTERM ab (`status=11/SEGV`). Das
  geschah bei jedem Neustart am 2026-10-25, auch vor diesem Auftrag, und nicht
  im laufenden Betrieb. Der Kiosk startet danach normal. Dafür gibt es noch kein
  Ticket.
- `/health` liefert keine Version. Das Deploy-Skript liest sie anders aus.
