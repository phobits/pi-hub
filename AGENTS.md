# AGENTS.md

## Rolle

Du arbeitest in diesem Repository als principal-level Software Engineer für ein Embedded-nahes Smart-Display-Produkt.

Das Ziel ist kein generisches Webprojekt und kein Desktop-Dashboard, sondern ein dauerhaft laufender Home-Screen für schwache Hardware.

## Produktkontext

- Zielhardware: Raspberry Pi Zero 2 W
- RAM-Budget: 512 MB
- Display: 7" Touch, 1024x600
- Betriebsart: Kiosk, Fullscreen, dauerhafter Betrieb
- UX-Ziel: ruhig, hochwertig, gut lesbar, touch-optimiert, produktnah

Jede technische und visuelle Entscheidung muss sich dieser Hardware unterordnen.

## Harte Regeln

- Optimiere für das feste Zielgerät, nicht für allgemeine Desktop-Layouts.
- Bevorzuge einfache, robuste, Pi-freundliche Lösungen.
- Vermeide unnötige Client-Komplexität, unnötige Re-Render und unnötigen JavaScript-Ballast.
- Führe keine schweren Frameworks oder Build-Systeme ein, wenn sie nicht zwingend notwendig sind.
- Secrets und externe Integrationen gehören nicht direkt ins Frontend.
- Fehler einzelner Provider dürfen nie die gesamte UI destabilisieren.
- Sichtbare deutsche Texte müssen echte Umlaute verwenden.

## Architekturleitplanken

- Backend: Python + Flask + Waitress
- Frontend: servergerenderte HTML/CSS/Vanilla-JS-Oberfläche
- Datenfluss: Provider aggregieren serverseitig, die UI konsumiert nur den vereinfachten lokalen State
- Caching: `last known good` und lokaler Bild-Cache sind Teil des Produkts, kein nachträglicher Zusatz
- Deployment: systemd-tauglich, kiosk-tauglich, unauffällig im Dauerbetrieb

Wenn du eine Änderung planst, die diese Leitplanken verletzt, brauchst du eine sehr gute hardwarebezogene Begründung.

## Aktueller UI-Maßstab

Die UI hat zwei Screens, gewechselt per horizontalem Wisch (rein client-seitig, beide liegen im selben Dokument — kein Router, kein zweiter Seitenaufruf). Es gibt **bewusst keine Seitenindikatoren**: Der Inhalt sagt eindeutig, auf welchem Screen man ist, und gestapelte Punkte widersprachen der horizontalen Wischrichtung. Nicht wieder einführen.

**Screen 1 — Homescreen (Standard), Stand „Glance“.** Keine Karten,
keine Rahmen: Hierarchie entsteht aus Schriftgröße, Gewicht und Abstand, und nur
Antippbares bekommt eine Fläche (Musik-Leiste, nächster Termin). Die Zielhierarchie ist:

- links (612 px): Uhr-Bühne, Datum, Wetter jetzt + drei Tage — bewusst groß, das ist die Kernfunktion aus 1–2 m
- rechts (328 px): Statusleiste (36 px, immer reserviert), Termine, unten die Musik-Leiste

Die Gewichtung war früher umgekehrt (Termine breit). Ein Termin besteht aus Uhrzeit und kurzem Titel und braucht die Breite nicht; die Uhr schon. Nicht zurückdrehen.

**Screen 2 — Spotify-Steuerung.** Cover 380 px, Scrubbing mit Restzeit („−2:57“) statt Gesamtzeit, Transport- und Lautstärkeregler mit Symbol, die nächsten vier Titel der Warteschlange mit Dauer sowie das Geräte-/Playlist-Overlay. Screen 2 besitzt die **gesamte** Spotify-Interaktion — Screen 1 ist reine Anzeige, es gibt nichts mehr zu synchronisieren. Die Warteschlange wird nur geladen, solange dieser Screen sichtbar ist; der Screensaver setzt die UI immer auf den Homescreen zurück. Zurück geht es nur per Wisch — kein „Start“-Knopf, bewusst.

Wichtige aktuelle UI-Entscheidungen:

- **Ein Maßstab.** `app.css` kennt genau eine Auflösung: 1024×600. Keine Media-Queries, kein `clamp()`, keine `vw`. Jede Größe steht einmal im CSS. Wer das Panel wechselt, ändert dort Zahlen — nicht eine zweite Leiter.
- **Feste Bühnen.** Die Uhr-Bühne ist 300 px hoch (Flip, LCD, Puls) oder 376 px (Wortuhr, Analog). Darunter 40 px Datum, der Rest Wetter. Kein Face hinterlässt Leerraum, und das Wetter springt beim Face-Wechsel nicht.
- Die Wortuhren und die Analoguhr sind quadratisch und damit höhen-, nicht breitengebunden. Sie bekommen die höhere Bühne und dafür das **kompakte Wetter**: eine 56-px-„Jetzt“-Zeile und eine 40-px-Leiste mit drei Tages-Chips (Symbol, Name, Max/Min). Die Vorschau entfällt nicht mehr — sie kostet dort nichts.
- keine Überschriften („Termine“, „Wetter Zuhause“, „Startbildschirm“): Der Inhalt sagt, was er ist. Datum in Normalschrift „Donnerstag, 15. Oktober“, keine Versalien.
- **Keine Status-Badges, kein technisches Vokabular.** Veraltete Daten heißen „Stand HH:MM“ (Bernstein) an der Stelle, die veraltet ist: beim Wetter in der Zweitzeile, beim Kalender als „Offline · Stand HH:MM“ in der Statusleiste. HH:MM ist das `updated_at` des Snapshots, also die letzte gute Abfrage. Nur `stale`/`error` lösen das aus; `empty` (Provider nicht konfiguriert) zeigt nichts.
- **Der nächste Termin ist hervorgehoben**: eigene Fläche, 24-px-Titel, rechts die relative Zeit — „jetzt“ während er läuft, „in N Min“ bis 60 Minuten davor, darüber nichts (die Uhrzeit steht ohnehin links). Nächster = erster nicht vergangener, nicht ganztägiger Termin über alle Tage. Vergangene Termine werden abgeblendet, pro Tag bleibt höchstens der jüngste stehen. Die relative Zeit wird im Minuten-Tick als Textknoten geschrieben; erst wenn ein Termin endet oder der nächste wechselt, rendert die Liste neu.
- Kalender zeigt heute, morgen und übermorgen (Abschnittslabels „Heute · Donnerstag“), aber nur so viel wie in den Platz passt. Der hervorgehobene Termin zählt im Zeilenbudget doppelt (`section_extra_rows` in `compute_row_budget`, Python und JS gemeinsam).
- Termintitel sind **einzeilig mit Ellipse** — zweizeilige Titel verdoppeln die Zeilenhöhe und halbieren die Anzahl sichtbarer Termine.
- Gekürzt wird bei Gleichstand **von hinten** (`>=` in `compute_row_budget`). Abschnitte kommen chronologisch, und bei einem Termin pro Tag sind alle gleich groß — mit `>` fiele ausgerechnet *heute* weg und übermorgen bliebe stehen. Diese Regel nicht umdrehen. Innerhalb eines Tages fällt der abgeblendete vergangene Termin als Erstes.
- **Die Musik-Leiste hat vier Formen**, gewählt in `renderSpotify` und per `data-music` auf `#cards-column` gemalt: `playing` 96 px (Cover 72 px, Titel, „Artist · Gerät“, Fortschrittsbalken), `idle` 60 px („Musik starten“, Spotify verbunden, nichts läuft), `error` 60 px („Spotify · Nicht erreichbar · zuletzt HH:MM“, Snapshot `error`) und `hidden` 0 px (nicht verbunden — die Termine bekommen die Zeile). `stale` ist ein verpasster Refresh und ändert nichts. Alle drei Grid-Zeilen der rechten Spalte sind feste Längen — **bewusst nicht inhaltsabhängig**, weil `renderCalendar` sein Zeilenbudget aus `clientHeight` einer bereits geleerten Liste zieht und eine `fit-content`-Zeile dabei kollabiert.
- Die ganze Leiste ist tippbar und öffnet Screen 2. Transport, Lautstärke, Gerätewahl und Zeiten liegen **ausschließlich** dort.
- **Nichts bewegt sich ohne Grund.** Keine blinkenden Doppelpunkte (LCD, Puls), keine Infinite-Animation außer dem Alarm. Flip-Animation und Analog-Sekundenzeiger laufen nur, solange das Face sichtbar ist. Flip-Ziffern tragen `box-shadow`, nicht `filter: drop-shadow` — ein Filter ist ein eigener Raster-Layer pro Ziffer.
- Die Hero-Uhr ist per Touch umschaltbar zwischen `flip` (Standard), `lcd`, `pulse`, `qlocktwo`, `qlocktwo-ooe` und `analog`. Start-Face kommt aus `app.watch_face` bzw. `APP_WATCH_FACE`, die Nutzerwahl persistiert im `localStorage` — kein Server-Roundtrip.
- Display-Größen (Flip-Ziffern, Puls, Temperatur, Spotify-Titel, Screensaver-Uhr) stehen in `"Inter Display", "Inter"`. Hat das Gerät kein Inter Display (fonts-inter < 4.0), fällt es auf Inter zurück — gleiche Metrik, etwas breitere Glyphen, kein Bruch.

**Schnellmenü (Sheet von unten).** Wisch nach oben oder Tipp auf den Griff am
unteren Rand öffnet ein Sheet mit Timer, Uhr-Stil, Nachtmodus, einem Chip, der
den Screensaver sofort startet (normaler Tipp, kein Halten — harmlos und per
Tipp auf die Uhr rückgängig), und den beiden Power-Aktionen. Der Griff (44×4 px, Trefferfläche 120×26 px) ist **kein**
Seitenindikator: Er zeigt eine Kante, die sich hochziehen lässt, und liegt in
der Body-Padding-Zone, damit er auf beiden Screens nichts überdeckt. Power-Aktionen
nur mit 2-s-Halten, nie per Tipp; ohne `APP_ALLOW_SHUTDOWN=true` sind sie
deaktiviert und sagen warum.

**Timer.** Ein laufender Timer ist ein Chip in der Statusleiste der rechten
Spalte (überdeckt nie die Uhr) und eine Zeile im Screensaver — beides tabellarische Ziffern, 1-Hz-Tick nur solange ein Timer
lebt. Der Alarm ist Vollbild, pulsiert per Opacity (billig) und endet von selbst
nach 10 Minuten; er hat Vorrang vor dem Screensaver. Ohne Lautsprecher bleibt er
rein visuell — keinen Ton „vorbereiten“.

**Nacht.** Im Nachtfenster ändern sich nur Farben und die Deko-Gradienten,
nichts bewegt sich. Der Screensaver zeigt dann keine Fotos, sondern eine dunkle
Uhr auf Schwarz. Das ist das Maximum, das Software auf diesem Panel kann — das
Backlight hängt an der Display-Platine, nicht am Pi.

**Screensaver-Gesten.** Wisch nach links zeigt das nächste Foto, nach rechts
das vorige (Verlauf der letzten zehn Pfade im Frontend, der Server-Runde ist
das egal). Ein Tipp aufs Foto tut **nichts** — sonst würde jeder Wisch den
Screensaver beenden. Geweckt wird per Tipp auf die Uhr (Foto- und
Flip-Uhr, Trefferfläche 32 px größer als die Uhr, per Geometrie geprüft, weil
`.screensaver__info` `pointer-events: none` ist) oder per Doppeltipp irgendwo.
Nachts gibt es nichts zu blättern, dort weckt **jeder** Tipp. Alles löst beim
Aufsetzen aus, wie früher der einfache Tipp. Eine Taste weckt
sofort, ein klingelnder Timer beendet ihn von selbst. Nachts und ohne Fotos
blättert der Wisch nicht.

Diese Entscheidungen nicht versehentlich zurückbauen.

## Failure-Mode-Denken

Bei jeder Änderung an Wetter, Kalender, Spotify, Screensaver oder Kiosk-Flow prüfen:

- Was passiert offline?
- Was passiert bei ungültigen Tokens oder fehlerhaften Provider-Antworten?
- Bleibt `last known good` nutzbar?
- Bleibt die UI ruhig und lesbar?
- Entsteht ein leerer, kaputter oder technisch wirkender Zustand?

## Lokale Arbeitsweise

Nutze vor Abschluss möglichst diese Checks:

```bash
python3 -m compileall smart_display tests
python3 -m unittest discover -s tests
```

Für lokale UI-Prüfung:

- Demo-Server: `python3 -m smart_display.local_server`
- Zielansicht immer auch in `1024x600` prüfen: `python3 scripts/screenshot-states.py OUT_DIR`
  rendert alle Faces und Zustände (Leerlauf, Timer, Nacht, leer, offline,
  Screensaver, Sheet, Alarm) gegen den Demo-Server — headless Chromium, nicht
  WebKit; der Gerätecheck bleibt.

Wenn du Layout, Typografie, Zustandsdarstellung oder Informationsdichte änderst, validiere den Stand nicht nur logisch, sondern visuell.

## Code-Review-Maßstab

Wenn du dieses Projekt reviewst, priorisiere:

- echte Bugs
- Layoutprobleme auf 1024x600
- Touch- und Kiosk-Probleme
- Performance-Risiken auf dem Pi Zero 2 W
- fragile Zustandslogik
- schlechte Failure-Modes
- fehlende Tests bei kritischer Logik

Nicht priorisieren:

- rein subjektive Stilfragen ohne Produktrelevanz
- Desktop-zentrierte Optimierungen, die dem Zielgerät nichts bringen

## Bevor du größere UI-Änderungen machst

Prüfe immer:

1. Spart die Änderung wirklich Platz oder erhöht sie nur Komplexität?
2. Ist sie aus 1-2 Metern noch gut lesbar?
3. Ist sie mit Touch gut bedienbar?
4. Bleibt sie im leeren, stale oder error state sauber?
5. Ist sie auf dem Pi noch glaubwürdig leichtgewichtig?
