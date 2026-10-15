# Homescreen v2 — Review, Konzept, Plan

Stand: 2026-10-15 · Branch `feat/homescreen-v2` · Status: **Konzept, wartet auf Freigabe**

Mockups: `docs/konzept/homescreen-v2/mock/` (statisches HTML, 1024×600, gleiche Schrift wie das Gerät).
Screens: `docs/konzept/homescreen-v2/screens/neu/` (Konzept) und `screens/ist/` (aktueller Stand, Demo-Server).
Neu rendern: `python docs/konzept/homescreen-v2/mock/render.py <mock-dir> <out-dir>` (braucht Playwright).

---

## 1. Review des aktuellen Frontends

Grundlage: `index.html` (384 Zeilen), `app.css` (2793), `app.js` (3130), Demo-Server bei 1024×600 in headless Chromium.
Was gut ist und bleibt: Datenfluss, Render-Dedup pro Sektion, Zeilenbudget im Kalender, Watch-Face-Logik, Screensaver, Nacht, Timer, Sheet-Mechanik.

### Layout und Hierarchie

| # | Befund | Beleg |
|---|---|---|
| L1 | Das Hero-Panel ist inhaltsabhängig hoch, die Faces sind es nicht. Bei `pulse` bleiben ~170 px Leerraum unter der Wettervorschau, bei `flip` 50 px, bei den Wortuhren 0. Der Screen sieht je nach Face anders „fertig" aus. | `screens/ist/ist-home-pulse.png` |
| L2 | Zwei Container-Ebenen: Karten mit 1-px-Rahmen, 36-px-Radius und Schatten auf einem Hintergrund mit Radial-Gradienten. Das ist Dashboard-Optik; ein Panel aus 1–2 m braucht Inhalt, keine Rahmen. | `app.css:72–104` |
| L3 | Redundante Beschriftung: „Wetter Demo Zuhause" über einer 62-px-Temperatur, „Termine" über einer Liste mit „Heute · Donnerstag", Datum in Versalien mit 0,22 em Sperrung. | `app.css:507`, `:544`, `index.html` |
| L4 | Leerlauf-Spotify-Kachel ist 180 px hoch für den Text „Musik starten" — 30 % der rechten Spalte für ein inaktives Feature. | `app.css:1316` |
| L5 | Timer-Chip liegt absolut über der vierten Flip-Ziffer. | `app.css` `.timer-chip`, `screens/ist/ist-home-timer.png` |
| L6 | Termine haben keine Gewichtung: ein vergangener Termin (09:00) sieht um 17:42 genauso aus wie der nächste. Es gibt keine relative Zeit („in 48 Min"). | `app.js:1183` `createCalendarRow` |

### Zustände

| # | Befund | Beleg |
|---|---|---|
| Z1 | Status-Pills sagen „Cache" / „Fehler" / „Leer" — technisches Vokabular, ohne Zeitstempel. Der Nutzer will wissen, *wie alt* die Daten sind. | `app.js:1102` `setStatus` |
| Z2 | Spotify-Fehler landen in der Artist-Zeile der Kachel. Beim Kalender bleibt die letzte Liste zwar stehen (Store hält `last known good`), aber nichts zeigt an, dass sie alt ist; nur bei leerer Liste erscheint der Fehlertext. | `app.js:1442`, `:1317` |

### Screen 2 (Spotify)

| # | Befund |
|---|---|
| S1 | Kopfzeile „SPOTIFY / Wiedergabe" ist Rauschen; der Screen erklärt sich über das Cover. |
| S2 | Zeitangaben 11 px, Queue-Untertitel 11 px — aus 1 m nicht lesbar. (`app.css` `.spotify-detail__seek-times`) |
| S3 | Lautstärke-Slider ohne Symbol, Prozentwert sitzt links unter dem Cover statt am Slider. |
| S4 | Linke Spalte: 400-px-Cover, darunter halbe Spalte leer. Kein Rückweg außer Wischen (kein Affordance). |

### Code / Performance

| # | Befund | Beleg |
|---|---|---|
| C1 | Zwei vollständige Größenleitern für ein Gerät mit genau einer Auflösung: Basisregeln für Desktop, dann `@media (max-width: 1100px) and (max-height: 700px)` überschreibt ~1650 Zeilen lang. 29 `clamp(..vw..)`, obwohl 1 vw auf dem Gerät immer 10,24 px ist. Jede Größe existiert zweimal und muss zweimal gepflegt werden. | `app.css:1139`, `:1120` |
| C2 | `lcd-colon-blink` und `pulse-colon-blink` sind `infinite`-Animationen → bei diesen Faces repaintet das Panel 24/7, auch nachts. | `app.css:344`, `:379` |
| C3 | `filter: drop-shadow` auf jeder Flip-Ziffer (4×) — ein Filter-Layer pro Ziffer, bei jedem Flip neu gerastert. | `app.css:203` |
| C4 | Nicht kritisch, zur Kenntnis: `app.js` ist ein 3130-Zeilen-IIFE. Für „kein Build-Step" in Ordnung; die Aufteilung in 3–4 `<script defer>`-Dateien ohne Bundler wäre möglich, ist aber kein Teil dieses Konzepts. | |

---

## 2. Konzept „Glance"

### Prinzipien

1. **Inhalt statt Container.** Keine Karten, keine Rahmen. Hierarchie durch Schriftgröße, Gewicht und Abstand. Fläche bekommt nur, was antippbar ist (Musik-Leiste, nächster Termin).
2. **Fixer Canvas.** 1024×600 ist bekannt. Ein Satz Werte, keine Media-Queries, keine `vw`-Clamps. Spalten 612 / 328 px, Spaltenabstand 32 px, Rand 32 px.
3. **Jede Fläche hat einen festen Platz.** Uhr-Bühne 300 px (Flip/LCD/Puls) bzw. 376 px (Wortuhr/Analog, mit kompaktem Wetter). Kein Face hinterlässt Leerraum.
4. **Zeit ist relativ.** Der nächste Termin ist hervorgehoben und trägt „in 48 Min"; vergangene Termine werden abgeblendet (38 % Opazität). Eine Minute Rechenaufwand pro Tick, kein Request.
5. **Zustand als Zeitstempel, nicht als Badge.** Veraltet = „Stand 15:10" in Bernstein an der Stelle, die veraltet ist. Offline = eine Zeile oben rechts „Offline · Stand 16:50" + Punkt. Keine Pills, kein „Fehler".
6. **Leerlauf nimmt keinen Platz.** Musik-Leiste: 96 px bei Wiedergabe (Cover 72 px, zwei Zeilen, Fortschritt), 60 px im Leerlauf („Musik starten" mit Play-Symbol). Der Platz geht an die Termine.
7. **Nichts bewegt sich ohne Grund.** Kein Blinken, keine Infinite-Animation. Flip-Animation bleibt, Analog-Sekundenzeiger bleibt (1 Hz, nur sichtbar).
8. **Typografie:** Inter, Display-Größen in Inter Display (fällt sauber auf Inter zurück, wenn der Schnitt fehlt). Ziffern tabular. Datum in Normalschrift „Donnerstag, 15. Oktober", keine Versalien.

### Was bleibt (bewusst)

- Links 2/3 Uhr + Wetter, rechts 1/3 Termine + Musik (AGENTS.md).
- Alle sechs Faces, Tipp auf die Uhr wechselt, Auswahl im Sheet, `localStorage`.
- Wischen links → Spotify, hoch → Sheet. Kein Seitenindikator.
- Zeilenbudget im Kalender (`compute_row_budget`, Kürzung von hinten).
- Wortuhren ohne Vorschau-Kacheln — aber die Vorschau bleibt als Inline-Zeile erhalten (eine Zeile, 3 Tage), weil Platz dafür da ist.
- Screensaver, Nacht-Palette, Timer-Alarm, Power-Overlay unverändert in der Logik; nur Farben/Typo werden angeglichen.

### Was sich ändert (gegenüber AGENTS.md, braucht dein Ja)

| Entscheidung bisher | Neu | Warum |
|---|---|---|
| Leerlauf-Kachel 180 px als „beschrifteter Knopf" | 60-px-Leiste | 60 px ist ein vollwertiges Touch-Ziel. Die 120 px gehen an einen weiteren Termin-Tag. |
| Timer-Chip im Hero oben rechts | Chip in der Statusleiste der rechten Spalte (36 px, immer reserviert) | Überdeckt nie die Uhr. Die Leiste trägt auch Offline/Stand. |
| Status-Pills (versteckt bei ok) | Zeitstempel inline, kein Pill-Element | Lesbar, nicht technisch. |
| Wortuhr: keine Vorschau | Vorschau als Inline-Zeile | Grid bleibt 376 px (größer als heute 355), Vorschau kostet nichts. |
| Kalender veraltet: kein Hinweis | „Offline · Stand HH:MM" in der Statusleiste, Liste bleibt | Alter der Daten sichtbar machen, ohne die Liste zu stören. |

### Screens

Alle in `screens/neu/`, 1024×600, Demo-Daten (17:42, Wien 14°).

| Datei | Zeigt |
|---|---|
| `v2-home-flip.png` | Standard: Flip, Wiedergabe läuft, 2 Tage Termine, nächster Termin hervorgehoben |
| `v2-home-pulse.png` | Puls 236 px Inter Display |
| `v2-home-lcd.png` | LCD 560 px breit, Bernstein |
| `v2-home-qlocktwo.png` / `-ooe.png` | Wortuhr 376 px + kompaktes Wetter in einer Zeile |
| `v2-home-analog.png` | Analog 360 px |
| `v2-home-idle-timer.png` | Leerlauf-Musik (60 px), Timer-Chip, 3 Tage Termine |
| `v2-home-night.png` | Nacht: Schwarz, Grau, keine Gradienten, keine Schatten |
| `v2-home-empty.png` | Keine Termine, zweizeiliger Leerzustand |
| `v2-home-offline.png` | Offline-Leiste, Spotify nicht erreichbar in der Leiste, Termine bleiben |
| `v2-home-stale-sync.png` | Wetter veraltet „Stand 15:10" |
| `v2-spotify.png` | Screen 2: Cover 380 px, Titel 40 px, Play 84 px, Restzeit statt Gesamtzeit, Queue mit Dauer, „Start"-Rückweg |
| `v2-sheet.png` | Sheet in vier Spalten: Timer · Uhr · Nacht · Gerät |

---

## 3. Umsetzungsplan

Alles auf `feat/homescreen-v2`, kein Merge ohne Freigabe. Reihenfolge so gewählt, dass nach jedem Schritt `compileall` + `unittest` grün sind und das Panel per Demo-Server prüfbar ist.

### Phase 1 — Fundament (CSS neu, Markup Home)

- `app.css` **neu schreiben** (Ziel ≤ 1400 Zeilen): Tokens, ein fester Maßstab, keine Media-Queries, keine `clamp()`. Alte Datei wird ersetzt, nicht erweitert.
- `index.html`: Home-Markup nach Konzept. Alle `getElementById`-Knoten aus `app.js` bleiben erhalten oder werden in `app.js` mitgezogen (`tests/test_home_template.py` erzwingt das).
- Faces: Markup unverändert, nur Größen. Wortuhr-Grid 376 px, Analog 360 px, Flip 122×200, LCD 560 px, Puls 236 px.
- Screensaver, Alarm, Power-Overlay, Toast, Picker: Typo/Farben auf die Tokens, Struktur bleibt.

### Phase 2 — Verhalten Home (`app.js`)

- `renderCalendar`: „nächster Termin" bestimmen (erster mit `ends_at > now`, sonst `starts_at > now`), als `.next` rendern mit relativer Zeit; vergangene als `.past`. Relative Zeit im Minuten-Tick aktualisieren (nur Textknoten, kein Re-Render). Zeilenbudget: `.next` zählt als 2 Zeilen.
- `renderWeather`: neue Knoten; „Stand HH:MM" aus `snapshot.updated_at` bei `stale`/`error`.
- `renderSpotify`: Leiste statt Kachel; `is-spotify-active` bleibt der eine Schalter; Fehler → zweite Zeile „Nicht erreichbar · zuletzt HH:MM".
- Statusleiste: Timer-Chip (vorhandene Logik, neuer Platz) + Offline-Zeile, abgeleitet aus den Snapshots aller Provider.

### Phase 3 — Screen 2 und Sheet

- Spotify-Detail nach `v2-spotify.png`: Restzeit (`−2:57`), 15-px-Zeiten, Lautstärke-Symbol + Wert rechts, Queue mit Dauer (`duration_ms` liegt im Queue-Payload, `spotify_provider.py:223`), „Start"-Button oben links (`setActiveScreen("home")`).
- Sheet vier Spalten.

### Phase 4 — Python-Seite (klein)

- `routes.py`: keine Änderung am State nötig. Optional `initial_next_event` serverseitig für den ersten Frame — nur wenn das Flackern auf dem Gerät sichtbar ist.
- `calendar_layout.compute_row_budget`: Gewicht 2 für die hervorgehobene Zeile, Python und JS gemeinsam ändern, Tests anpassen.

### Phase 5 — Validierung

- `python3 -m compileall smart_display tests` · `python3 -m unittest discover -s tests`
- Visuell: Playwright-Skript (`mock/render.py` als Vorlage) gegen den Demo-Server, alle Faces × {Wiedergabe, Leerlauf, Nacht, Timer, leer, offline}. Headless Chromium, nicht WebKit — Gerätecheck danach per `deploy-pi.sh` (nur mit Freigabe).
- Gerät: prüfen, ob `Inter Display` installiert ist (`fc-list | grep "Inter Display"`). Falls nicht, fällt das Konzept auf `Inter` zurück — sichtbar nur in der Zeichenbreite der Display-Größen.

### Aufwand und Risiko

| Phase | Umfang | Risiko |
|---|---|---|
| 1 | CSS ~1300 Zeilen neu, Template Home | Mittel: Alle Knoten-IDs müssen erhalten bleiben. Test deckt das ab. |
| 2 | ~250 Zeilen JS geändert | Niedrig–mittel: Zeilenbudget-Logik wird angefasst, Tests vorhanden. |
| 3 | ~150 Zeilen JS, Template Screen 2 | Niedrig. |
| 4 | ~40 Zeilen Python + Tests | Niedrig. |
| 5 | Skript | Niedrig. WebKit/Cog bleibt ungeprüft, bis es auf dem Gerät läuft. |

Nicht im Konzept: neue Provider-Daten (stündliches Wetter, Wetter-Detailscreen). Wäre Phase 6, nach Freigabe gesondert.

---

## 4. Offene Fragen

1. Die fünf Abweichungen in „Was sich ändert" — alle ja, oder welche nicht?
2. Musik-Leiste im Leerlauf: 60 px wie im Mockup, oder ganz ausblenden, solange Spotify nicht verbunden ist?
3. Screen 2: „Start"-Rückweg oben links behalten, oder reines Wischen wie bisher?
4. Nächster Termin: relative Zeit nur bis 60 Minuten („in 48 Min"), darüber Uhrzeit? Mockup zeigt nur den Minutenfall.
5. Sprache im Leerzustand: „Keine Termine / Auch morgen und Samstag ist nichts eingetragen." — passt der Ton?
