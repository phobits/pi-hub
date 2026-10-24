# Changelog

Alle nennenswerten Änderungen an diesem Projekt werden hier dokumentiert.

Das Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
die Versionierung an [Semantic Versioning](https://semver.org/lang/de/).

Dieser Changelog wurde rückwirkend aus der Git-Historie und den Release-Tags
rekonstruiert (Stand v0.4.0). Ältere Einträge fassen daher zusammen, statt jeden
Commit einzeln aufzuführen.

## [Unreleased]

## [0.9.2] - 2026-10-24

### Behoben

- Lightroom-Screensaver: Das Titelbild eines geteilten Albums wurde zusätzlich
  zu den Album-Fotos als eigenes Bild gespeichert und lief damit doppelt so oft
  in der Rotation. Sobald die Share-Seite eine Album-API nennt, zählt nur noch
  deren Liste.
- Lightroom-Screensaver: Fiel die Album-API aus (Timeout, 5xx, zu große oder
  leere Antwort), blieb nur das Titelbild übrig und alle anderen gecachten
  Album-Fotos wurden gelöscht — danach lud das Gerät ein Foto pro
  Refresh-Intervall nach, bei 30 Minuten also stundenlang. Jetzt bleibt der
  Cache stehen und die Kachel geht auf `stale`.

## [0.9.1] - 2026-10-24

### Behoben

- Lokale Screensaver-Fotos werden wie die Lightroom-Bilder auf 1024×600
  normalisiert (Kopie unter `data/screensaver/local/`, das Original bleibt
  unangetastet) — ein Handyfoto in voller Auflösung kostete WPE sonst rund
  96 MB pro Überblend-Slot. Dateinamen mit `#` oder `%` funktionieren, macOS-
  `._*`-Dateien und andere Dotfiles zählen nicht mehr als Fotos, `foto.jpg` und
  `foto.png` bekommen getrennte IDs, und eine unter gleichem Namen ersetzte
  Datei wird erkannt. Die Rotation ist threadsicher. Decodiert wird im
  Provider-Thread mit Zeitbudget, ein Request decodiert höchstens ein neues
  Foto; fertige Kopien überleben Neustarts, werden atomar geschrieben und
  bleiben nach dem Ersetzen fünf Minuten abrufbar. Nicht-JPEGs über 12 Mpx
  werden übersprungen, ein neu hinzukommendes Foto mischt die laufende Runde
  nicht neu. (#5)
- Screensaver-Status: Ein leeres Lightroom-Album mit lokalen Fotos meldet `ok`
  statt `empty`; Demo-Bilder machen aus einem Abruffehler einer frischen
  Installation kein `stale` mehr. (#5)
- zram auf Raspberry Pi OS 13 (Trixie): `install-pi.sh` konfiguriert das
  mitgelieferte `rpi-swap` auf 128 MB reines zram ohne Swap-Datei auf der
  SD-Karte, statt `zram-tools` daneben zu installieren, das an jedem Boot
  scheiterte. (#6)
- CHANGELOG-Vergleichslinks ab 0.6.0 ergänzt, fehlender Tag `v0.8.0`
  nachgetragen. (#7)

## [0.9.0] - 2026-10-24

### Geändert — Homescreen v2 „Glance“

- **Homescreen neu gebaut.** Keine Karten, keine Rahmen, keine Überschriften;
  Hierarchie aus Größe, Gewicht und Abstand. Links 612 px Uhr-Bühne (300 px,
  Wortuhr/Analog 376 px), Datum „Donnerstag, 15. Oktober“, Wetter jetzt + drei
  Tage. Rechts 328 px: Statusleiste, Termine, Musik-Leiste. Konzept und
  Mockups unter `docs/konzept/homescreen-v2.md`.
- **Nächster Termin hervorgehoben** mit relativer Zeit („jetzt“, „in 48 Min“
  bis 60 Minuten); vergangene Termine abgeblendet, pro Tag bleibt höchstens
  der jüngste. Der hervorgehobene Termin zählt im Zeilenbudget doppelt
  (`compute_row_budget(section_extra_rows=…)`).
- **Zustand als Zeitstempel statt Badge**: „Stand HH:MM“ beim Wetter, „Offline ·
  Stand HH:MM“ in der Statusleiste, „Spotify · Nicht erreichbar · zuletzt HH:MM“
  in der Musik-Leiste. Die Status-Pills („Cache“, „Fehler“, „Leer“) sind weg.
- **Musik-Leiste statt Kachel**: 96 px bei Wiedergabe, 60 px im Leerlauf,
  ausgeblendet solange Spotify nicht verbunden ist. Der Timer-Chip sitzt in der
  Statusleiste statt über der Uhr.
- **Spotify-Screen**: Restzeit statt Gesamtzeit, 15-px-Zeiten, Lautstärke mit
  Symbol und Wert am Regler, Warteschlange mit Dauer (`duration_ms` im
  Queue-Payload), keine Kopfzeile. Sheet in vier Spalten (Timer · Uhr · Nacht ·
  Gerät).
- **`app.css` neu geschrieben** für genau eine Auflösung: keine Media-Queries,
  kein `clamp()`, keine `vw`. 2793 → ~2300 Zeilen, jede Größe einmal. LCD- und
  Puls-Doppelpunkt blinken nicht mehr (kein 24/7-Repaint), Flip-Ziffern tragen
  `box-shadow` statt `filter: drop-shadow`.
- Display-Größen in `Inter Display` mit Fallback auf `Inter`.
- `scripts/screenshot-states.py`: Playwright-Rendering aller Faces und Zustände
  gegen den Demo-Server (Entwicklungshilfe, kein Ship-Gate).

### Behoben

- Review-Befunde aus #4: Watchdog-Ping prüft den konfigurierten Host statt
  immer `127.0.0.1`; Ping-Kadenz `WATCHDOG_USEC/3` statt `/2`, damit ein
  verpasster Health-Probe keinen Neustart auslöst; „Timer nicht gefunden“
  liefert auf allen Timer-Routen 404 und das Frontend prüft den Status statt
  den Text; `StateStore` serialisiert pro Mutation nur noch einmal;
  `parseHhmm` spiegelt `night.parse_hhmm` exakt; `postAction`/`postJson`
  teilen sich eine Hilfsfunktion; `TimerService` liest Store und Uhr nicht
  mehr doppelt.

### Hinzugefügt

- Dauerhafte lokale Screensaver-Fotos aus `data/screensaver-local/` werden
  gemeinsam mit dem Lightroom-Cache angezeigt. Lightroom-Abgleiche und
  Deployments lassen diese nutzereigenen Dateien unangetastet.
- Die Screensaver-Auswahl mischt alle verfügbaren Fotos rundenweise. Jedes Bild
  erscheint genau einmal, bevor neu gemischt wird; direkte Wiederholungen sind
  auch am Übergang zwischen zwei Runden ausgeschlossen.
- **Schnellmenü** (Wisch nach oben oder Tipp auf den Griff am unteren Rand):
  Timer-Presets, Uhr-Stil, Nachtmodus, Neu starten/Ausschalten mit 2-s-Halten.
  Der Griff ist kein Seitenindikator — er markiert eine Fläche, die sich hochziehen
  lässt. (#2, #3)
- **Timer** (#3): bis zu drei Countdowns, persistiert in `last_good.json`
  (`DashboardState.timers`, additiv ohne Schema-Bump), Chip oben rechts im
  Hero, Restzeit im Screensaver, Vollbild-Alarm mit „Stopp“ und „+1 Min“.
  Rein visuell — das Panel hat keinen Lautsprecher. Ein nicht bestätigter
  Alarm verschwindet nach 10 Minuten. Endpunkte unter `/api/timers`.
- **Nachtfenster** (#2): `APP_NIGHT_FROM`/`APP_NIGHT_TO` (Standard 22:30–06:00)
  schalten eine gedimmte Palette und einen Screensaver ohne Fotos (dunkle Uhr auf
  Schwarz). Manuelles An/Aus im Schnellmenü, persistiert im `localStorage`.
- **Uhr über den Screensaver-Fotos** (#2): Zeit, Datum, Temperatur unten links
  als Textknoten, ein Update pro Minute.
- **Ausschalten/Neustart per Touch** (#2): `POST /api/system/{shutdown,reboot}`
  hinter `APP_ALLOW_SHUTDOWN=true` (Standard aus, der Demo-Server kann keinen
  Rechner herunterfahren) und einer sudoers-Regel für genau zwei Kommandos.
  Optional `dtoverlay=gpio-shutdown` (`SMART_DISPLAY_GPIO_SHUTDOWN=true`), damit
  Pin 5 gegen GND den gehaltenen Pi wieder weckt.
- **Dauerbetrieb** (#1): `smart-display.service` ist `Type=notify` mit
  `WatchdogSec=90`; `READY=1` erst nach dem Bind, `WATCHDOG=1` nur nach einem
  erfolgreichen lokalen `GET /health`. journald-Deckel (32 MB), Hardware-Watchdog
  (`RuntimeWatchdogSec=15`, `dtparam=watchdog=on`), zram-Swap 128 MB,
  `OOMScoreAdjust` (Backend −500, Kiosk +500). Alles über `install-pi.sh`.
- **Uhr-Hinweis** (#1): solange `systemd-timesyncd` nicht synchronisiert hat,
  steht „Uhr wird gestellt …“ unter dem Datum (`system.time_synced`).
- `scripts/measure_pi.py` (#1): Messbaseline auf dem Gerät — RAM, RSS je
  Prozess, CPU-Mittel, SD-Schreibvolumen, `/api/state`-Latenz, Restarts.
- `scripts/pi/test-panel-blank.sh` (#2): Hardware-Test, ob das Panel bei
  DPMS-Off dunkel wird.

### Geändert

- `/api/state` liefert unveränderte Polls per ETag als `304 Not Modified` ohne
  JSON-Body. Das spart dem WPE-Browser auf dem Pi JSON-Parsing und
  Re-Render, solange sich nichts geändert hat.
- `deploy-pi.sh` lässt gerätseitige Credential-Backups (`.env.*`,
  `.kiosk.env*`) bei `--delete` stehen.
- `StateStore` schreibt `last_good.json` nur noch bei inhaltlicher Änderung
  (ohne `progress_ms`, `generated_at`, `updated_at`), sonst höchstens alle
  300 s, plus `flush()` beim Beenden (SIGTERM läuft jetzt über `atexit`).
  Vorher: ein Write pro Spotify-Refresh, ~2.900 am Tag. (#1)
- `install-pi.sh` hat einen achten Schritt (journald, Watchdog, zram, sudoers).

## [0.8.0] - 2026-07-19

### Geändert

- Die QLOCKTWO-Faces tauschen die Wettervorschau gegen Rastergröße: 300 → 355 px,
  Buchstaben 22 → 26 px, es bleibt der aktuelle Wert. Das Raster ist quadratisch
  und damit höhen-, nicht breitengebunden — die Chip-Reihe war die einzige Höhe,
  die zu holen war. Eine Wortuhr wird gelesen, nicht wie Zeiger überflogen; dort
  ist Größe unmittelbar Lesbarkeit.
- Die Analoguhr wächst aus demselben Grund von 245 auf 287 px.
- Die Spotify-Kachel im Leerlauf ist 180 statt 96 px hoch, mit dem Play-Glyph
  neben statt über dem Label und zentriertem Inhalt. Sie ist ein beschrifteter
  Knopf, kein leeres Feld — die flache Variante stammte noch aus der Zeit, als
  dort „Keine aktive Wiedergabe“ stand. Nebeneffekt: Die Termine-Karte bekommt
  nicht länger Fläche zugewiesen, die sie bei wenigen Terminen nicht füllen kann.
- `scripts/deploy-pi.sh` startet die systemd-Units nur noch, wenn der rsync
  tatsächlich etwas übertragen hat, und kennt `--force-restart`. Doku, Tests und
  `*.md` sind vom Sync ausgeschlossen, ein reiner Doku-Ship lässt das Panel also
  an. Exit-Code `3` bedeutet: Pi nicht erreichbar, nichts deployed.
- `.kiosk.env` steht jetzt in `.gitignore`. Die Datei lebt nur auf dem Gerät,
  aber eine Kopie im Repo würde `git add -A` mitnehmen.

Alle sechs Watch-Faces auf 1024×600 gegen Clipping geprüft: 30 px Restluft bei
der Analoguhr als knappstem Fall, bis 185 px bei `pulse`.

## [0.7.0] - 2026-07-19

### Geändert

- Die UI ist auf **Inter** umgestellt (`fonts-inter`, neu in `install-pi.sh`),
  mit Noto Sans Display als Fallback. Grund war kein Geschmack: Noto Sans Display
  liefert nur Regular und Bold, also rendert jedes `font-weight` zwischen 500 und
  700 als Fett. Die UI wirkte deshalb durchgehend klotzig, und „weniger fett" war
  schlicht nicht einstellbar.
- Gewichte gestaffelt: 700 nur noch bei Uhr und Temperatur, 600 für Überschriften
  und mittelgroße Labels, 500 für kleinen Sekundärtext, 400 für Fließtext.
  Überschriften haben jetzt ein explizites Gewicht — sie sind `<h1>`/`<h2>` und
  liefen vorher auf den Browser-Default *bold*, ohne dass eine Deklaration im
  Stylesheet darauf hingedeutet hätte.
- Negatives Tracking nur noch oberhalb ~40 px. Bei 14–16 px zieht es auf diesem
  Panel die Punzen zu.
- Uhrzeiten, Temperatur, Vorschauwerte und die Lautstärkeanzeige laufen auf
  Tabellenziffern, damit sich ändernde Werte das Layout nicht verschieben.
- Termine sind luftiger: Zeilenabstand 6→10 px, Padding 9→13 px, Titel 16→18 px,
  Uhrzeit 14→16 px. Das kostet auf 1024×600 einen Tag — bewusst, weil
  Lesbarkeit aus 1–2 m auf diesem Panel die knappere Ressource ist.

### Behoben

- Bei Gleichstand kürzte `compute_row_budget` den **ersten** Abschnitt. Da
  Abschnitte chronologisch kommen und bei einem Termin pro Tag alle gleich groß
  sind, fiel damit ausgerechnet *heute* weg, während übermorgen stehen blieb.
  Gekürzt wird jetzt von hinten.
- `<button>` erbt die Dokumentschrift nicht, sondern fällt auf die UA-Schrift
  zurück — auf dem Pi eine Familie mit nur Regular und Bold. Sämtliche
  Picker-Buttons wären damit weiter fett gelaufen. Einmal global gesetzt statt
  pro Button-Regel kompensiert.
- Die Zeitspalte der Termine war mit 92 px zu schmal: `09:00–10:30` misst bei
  16px/600 mit Tabellenziffern ~99 px und malte über den Spaltenrand hinaus in
  den Abstand zum Titel. Jetzt 104 px.
- `test_section_can_be_trimmed_to_zero_and_drops_label` kürzte nie etwas auf
  null — die Fixture ergab immer `[1,1,1]` und die Assertions waren zu locker,
  um das zu bemerken. Der Pfad ist jetzt tatsächlich abgedeckt.

## [0.6.0] - 2026-07-19

### Geändert

- Homescreen-Aufteilung gedreht: links **2/3** (Uhr, Datum, Wetter), rechts
  **1/3** (Termine, Spotify). Vorher 38/62 zugunsten der Termine. Ein Termin
  besteht aus Uhrzeit und kurzem Titel und brauchte die 613 px nie; die Uhr ist
  die Kernfunktion aus 1–2 m. Die Hero-Innenfläche wächst von 323 px auf 605 px.
- Uhr und Wetter entsprechend vergrößert (Flip-Ziffern 72→132 px breit,
  Temperatur 44→64 px, Vorschau-Chips größer). Die Clamp-Maxima greifen bei
  genau 1024 px, wie im bestehenden Idiom.
- Die Spotify-Kachel auf dem Homescreen ist jetzt reine Anzeige: vollflächiges
  Artwork mit Scrim, Titel, Interpret und ein Fortschrittsbalken. Die gesamte
  Kachel ist tippbar und öffnet den Spotify-Screen — auch im Leerlauf, wo sie
  „Musik starten“ zeigt. Bei laufender Session 250 px hoch, im Leerlauf 96 px.
- Termintitel sind einzeilig mit Ellipse. Zweizeilig hätte das Zeilenbudget in
  der schmalen Spalte auf 4 gedrückt, wodurch die „größten Abschnitt zuerst
  kürzen“-Regel **heute** komplett verworfen hätte.
- Zustand der rechten Spalte läuft über vererbte Custom Properties statt über
  konkurrierende Zustands-/Größenselektoren.

### Entfernt

- Transport-Controls, Lautstärkeregler, Geräte-Badge, Status-Badge und die
  Zeitangaben von der Homescreen-Spotify-Kachel. Alles davon liegt auf Screen 2.
  Das Geräte-/Playlist-Overlay wird dadurch über Screen 2 → „Musik wählen“
  erreicht. Fehler bleiben auf dem Homescreen sichtbar: Die Artist-Zeile fällt
  auf die Fehlermeldung des Snapshots zurück.

### Behoben

- Das Zeilenbudget des Kalenders war in genau dem Layout wirkungslos, für das es
  geschrieben wurde: `fit-content(48%)` machte die Kalenderzeile inhaltsabhängig,
  die vor der Messung geleerte Liste kollabierte, und der Null-Höhen-Fallback
  rendert alle Termine in eine überlaufende Box. Beide Grid-Zeilen sind jetzt
  inhaltsunabhängig, zusätzlich wird vor dem Leeren gemessen.
- `rowUnit` ignorierte den Flex-Gap der Liste (`offsetHeight` enthält ihn nicht),
  wodurch das Budget bei mehreren Zeilen überzeichnete. Vorher vom
  `fit-content`-Fehler verdeckt.
- Die QLOCKTWO-Faces liefen ~11 px über und wurden von `overflow: hidden`
  beschnitten. Ihr Raster ist quadratisch und damit höhengebunden — der neue
  300-px-Deckel behebt das.
- Eine Wischgeste, die auf dem bereits aktiven Screen endete, unterdrückte den
  synthetischen Klick nicht. Mit der großen Spotify-Kachel hätte ein solcher
  Fehlwisch navigiert.
- `.spotify-artwork` wurde in der Kiosk-Media-Query nie wirksam, weil die
  Zustandsregel höher spezifisch war. Mit den alten Regeln entfallen. Die
  Kachelhöhe läuft jetzt über ein `--tile-h-idle`/`--tile-h-active`-Paar, das ein
  Breakpoint nicht mehr halb definieren kann — Media-Queries erhöhen die
  Spezifität nicht, weshalb dieselbe Falle sonst sofort wieder zuschnappt.

## [0.5.1] - 2026-07-19

### Behoben

- Zurückwischen vom Spotify-Screen funktionierte auf dem Gerät nicht. Die
  Geste ist jetzt gegen Engine-Unterschiede gehärtet, statt Pointer-Events
  in Blink-Semantik vorauszusetzen:
  - Bricht die Engine die Geste ab (`pointercancel`/`touchcancel`, weil sie
    den Touch für eigenes Scrollen oder eine Navigationsgeste übernimmt),
    wird der Wisch trotzdem ausgeführt, sofern er die Schwelle schon
    überschritten hatte. Vorher wurde er ersatzlos verworfen.
  - Touch-Events als zweiter Pfad, falls Pointer-Events für Touch
    unvollständig sind. Die Geste wird an die zuerst feuernde Familie
    gebunden, kann also nicht doppelt ausgelöst werden.
  - `SWIPE_MAX_MS` von 800 ms auf 1500 ms. Ein überlegter Wisch am
    Wandpanel ist langsamer als ein Handy-Flick.

## [0.5.0] - 2026-07-19

### Entfernt

- Die Seitenindikatoren. Sie waren vertikal gestapelt, obwohl horizontal
  geblättert wird, und zeigten nichts, was der Inhalt nicht ohnehin sagt.
  Mit ihnen fällt `--shell-gutter` weg — eine Kopplung an zwei unabhängig
  justierbare Grid-Splits, die zweimal in Folge repariert werden musste.

### Geändert

- Der Wisch startet jetzt überall, auch über Buttons und Karten: Ein
  horizontaler Zug blättert, ein Tipp löst weiterhin das getroffene Element
  aus. Ausgenommen bleiben die Regler, die die horizontale Achse selbst
  brauchen. Vorher blockierte jeder `button` die Geste — deshalb stand im
  README wörtlich „swipe on a non-interactive part".
- `setPointerCapture` auf der Stage entfällt. Pointer-Events blubbern ohnehin
  hoch, und ein Capture auf dem Vorfahren leitet die Compat-Maus-Events um,
  was Taps auf den jetzt wischbaren Buttons verschluckt hätte.

## [0.4.1] - 2026-07-19

### Behoben

- Die Seitenpunkte saßen auf beiden Screens auf einer Kartenkante statt in der
  Spaltenrinne. Ursache: Home (`0.76fr/1.24fr`) und Spotify-Screen
  (`0.78fr/1.22fr`) haben unterschiedliche Aufteilungen, die Punkte hängen aber
  absolut positioniert am gemeinsamen `.screen-stage` — ein einzelner Wert kann
  nicht für beide stimmen. `--shell-gutter` wird jetzt über
  `[data-active-screen]` pro Screen gesetzt (Home 38,1 %, Spotify 39,1 %);
  gemessen sitzen beide exakt mittig in der 12px-Rinne.

## [0.4.0] - 2026-07-18

### Hinzugefügt

- Zweiter Screen: vollflächige Spotify-Steuerung, erreichbar per Wisch oder
  Seitenpunkt. Großes Artwork, Scrubbing, Transport- und Lautstärkeregler
  (synchron mit dem Homescreen) sowie die nächsten vier Titel der Warteschlange.
  Beide Screens liegen im selben Dokument — kein Router, kein zweiter Seitenaufruf.
- `POST /api/spotify/seek` und `GET /api/spotify/queue`, beide loopback-geschützt.
  Die Warteschlange wird nur geladen, solange der Spotify-Screen sichtbar ist, und
  fällt bei Upstream-Fehlern auf das letzte gute Ergebnis zurück — begrenzt durch
  `_QUEUE_STALE_MAX_AGE_SECONDS`, damit keine stundenalten Titel als aktuell gelten.
- Podcast-Unterstützung: `refresh()` sendet `additional_types=episode`. Ohne diesen
  Parameter liefert Spotify `item: null` für Episoden — laufende Podcasts wurden
  dadurch als „Keine aktive Wiedergabe" angezeigt.

### Geändert

- Der Screensaver setzt die UI immer auf den Homescreen zurück.
- `/api/state` wird pro Sektion dedupliziert, statt über den gesamten State — ein
  Spotify-Update baut Wetter und Kalender nicht mehr neu auf.
- `last_good.json` wird kompakt statt eingerückt geschrieben (weniger SD-Karten-Writes).
- Chromium-Kiosk startet mit zusätzlichen Flags gegen Hintergrund-Netzwerkverkehr,
  Erstlauf-Dialoge und Benachrichtigungen.

### Behoben

- Kalender: Das Zeilenbudget wurde nach einem Spotify-Layoutwechsel nicht neu
  gemessen, wodurch Termine hinter ausgeblendeten Scrollbalken verschwanden.
- Kalender ließ sich nicht mehr per Touch scrollen (`touch-action: none` auf dem
  Screen-Container statt `pan-y`).
- Waitress läuft wieder mit vier Threads: Beim Öffnen der Geräteauswahl laufen zwei
  Spotify-Requests parallel, die sich denselben Host-Lock teilen — mit zwei Threads
  blockierten sie den `/api/state`-Poll und die Screensaver-Routen.
- Eine leere zwischengespeicherte Warteschlange zeigte die Statuszeile
  „zuletzt erfolgreich geladen" als Listeninhalt.
- Der Fortschrittsbalken sprang bei jedem Pause-Tap um bis zu ein Poll-Intervall
  zurück; die optimistische Aktualisierung übernimmt jetzt die interpolierte Position.
- Ein Seek wird verworfen, wenn der Titel während des Ziehens wechselt; Skips setzen
  den Seek-Hold zurück.
- Die Klick-Unterdrückung nach einem Wisch schluckte bis zu 500 ms lang echte Taps —
  jetzt einmalig mit 150 ms Rückfallgrenze.
- Fortschrittsanzeige und analoger Sekundenzeiger laufen nur noch auf dem sichtbaren
  Screen.
- Der Seek-Regler übernimmt die Regeln des Lautstärkereglers und damit die
  1024×600-Größen, die ihm fehlten.
- Die Seitenpunkte folgen der Spaltenaufteilung über `--shell-gutter`, statt einem
  fest verdrahteten Prozentwert, der nur zum Kiosk-Breakpoint passte.
- Genau ein `<main>` pro Dokument; die Screen-Container sind einfache `div`s.

## [0.3.1] - 2026-06-02

### Behoben

- Connect-Lautstärke wird schon bei `input` übernommen, nicht erst bei `change` —
  auf dem Touchpanel kam sonst oft gar kein `change` an.

## [0.3.0] - 2026-06-02

### Hinzugefügt

- Wiedergabe lässt sich aus dem Auswahl-Overlay heraus direkt auf einem
  Connect-Lautsprecher starten. Benötigt den Scope `playlist-read-private` —
  bei älteren Refresh-Tokens `scripts/spotify-auth.py` erneut ausführen.

## [0.2.2] - 2026-06-02

### Behoben

- Lautstärkeregler zentriert und bei Smartphone-Ausgabe deaktiviert, weil Spotify
  dort keine Fernsteuerung der Lautstärke erlaubt.

## [0.2.1] - 2026-06-02

### Geändert

- Rechte Spalte neu gewichtet: Termine kürzer, Spotify größer, sobald eine
  steuerbare Session läuft.

## [0.2.0] - 2026-06-02

### Hinzugefügt

- `scripts/spotify-auth.py` als OAuth-Helfer zum Erzeugen des Refresh-Tokens.
- Gehärtetes Pi-Setup: robusterer `install-pi.sh`-Bootstrap und Kiosk-Start.

### Geändert

- UI-Optimierung für 1024×600: kompaktere Kalenderzeilen, überarbeitetes
  Spotify-Layout, angepasste Typografie.
- Der Frontend-Poll ist vom Spotify-Refresh entkoppelt — `/api/state` ist
  prozessintern und damit billig, der Upstream-Refresh bleibt langsam.

## [0.1.0] - 2026-04-08

Erste Version. Nie getaggt; entstanden zwischen dem 2026-04-08 und 2026-04-15.

### Hinzugefügt

- Smart Display für Raspberry Pi Zero 2 W an einem 1024×600-Touchpanel im
  Kioskmodus: einzelner Flask-Prozess, serverseitig gerendertes HTML und
  Vanilla-JS ohne Build-Schritt.
- Provider für Wetter (Open-Meteo), Kalender (CalDAV/ICS), Spotify und
  Lightroom-Screensaver, jeder als eigener `ScheduledJob` mit eigenem Intervall.
- `StateStore` als einzige schreibbare Zustandsquelle, mit `last_good.json` und
  Schema-Versionierung samt Quarantäne bei Mismatch.
- Sechs Hero-Uhren: `flip`, `lcd`, `pulse`, `qlocktwo`, `qlocktwo-ooe` (oberöster-
  reichischer Dialekt) und `analog`. Auswahl persistiert im `localStorage`.
- Screensaver mit Crossfade, Pause-Gruppen im Scheduler und TTL-Watchdog, damit ein
  abgestürztes Frontend das Polling nicht dauerhaft anhält.
- Sicherheitsgrenzen: POSTs nur von Loopback, Waitress fest auf `127.0.0.1`, lauter
  Abbruch bei abweichendem Bind.
- `install-pi.sh` plus systemd-Units für Backend und Chromium-Kiosk.

[Unreleased]: https://github.com/phobits/pi-hub/compare/v0.9.2...HEAD
[0.9.2]: https://github.com/phobits/pi-hub/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/phobits/pi-hub/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/phobits/pi-hub/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/phobits/pi-hub/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/phobits/pi-hub/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/phobits/pi-hub/compare/v0.5.1...v0.6.0
[0.5.1]: https://github.com/phobits/pi-hub/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/phobits/pi-hub/compare/v0.4.1...v0.5.0
[0.4.1]: https://github.com/phobits/pi-hub/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/phobits/pi-hub/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/phobits/pi-hub/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/phobits/pi-hub/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/phobits/pi-hub/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/phobits/pi-hub/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/phobits/pi-hub/releases/tag/v0.2.0
[0.1.0]: https://github.com/phobits/pi-hub/commits/v0.2.0
