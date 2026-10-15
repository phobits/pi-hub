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

**Screen 1 — Homescreen (Standard).** Die Zielhierarchie ist:

- links (2/3 der Breite): Uhrzeit, Datum, Wetter mit kompakter Vorschau (3 Tage) — bewusst groß, das ist die Kernfunktion aus 1–2 m
- rechts (1/3): oben Termine, darunter die Spotify-Kachel

Die Gewichtung war früher umgekehrt (Termine breit). Ein Termin besteht aus Uhrzeit und kurzem Titel und braucht die Breite nicht; die Uhr schon. Nicht zurückdrehen.

**Screen 2 — Spotify-Steuerung.** Großes Artwork, Scrubbing, Transport- und Lautstärkeregler, die nächsten vier Titel der Warteschlange sowie das Geräte-/Playlist-Overlay. Screen 2 besitzt die **gesamte** Spotify-Interaktion — Screen 1 ist reine Anzeige, es gibt nichts mehr zu synchronisieren. Die Warteschlange wird nur geladen, solange dieser Screen sichtbar ist; der Screensaver setzt die UI immer auf den Homescreen zurück.

Wichtige aktuelle UI-Entscheidungen:

- keine separate Wetterkarte rechts
- keine überflüssigen Überschriften wie „Startbildschirm“
- Status-Badges werden im Normalzustand nicht angezeigt
- Kalender zeigt heute, morgen und übermorgen (Tagesüberschriften mit Wochentag, z. B. „Heute · Montag“), aber nur so viel wie in den verfügbaren Platz passt
- Termintitel sind **einzeilig mit Ellipse** — zweizeilige Titel verdoppeln die Zeilenhöhe und halbieren die Anzahl sichtbarer Termine.
- Die Termine sind bewusst **luftig statt vollständig**: größere Schrift und mehr Zeilenabstand schlagen einen zusätzlichen Tag, weil die Lesbarkeit aus 1–2 m auf diesem Panel die knappere Ressource ist. Auf 1024×600 bleiben dadurch rund vier Zeilen, also heute und morgen.
- Gekürzt wird bei Gleichstand **von hinten** (`>=` in `compute_row_budget`). Abschnitte kommen chronologisch, und bei einem Termin pro Tag sind alle gleich groß — mit `>` fiele ausgerechnet *heute* weg und übermorgen bliebe stehen. Diese Regel nicht umdrehen.
- Der Homescreen zeigt für Spotify nur noch Artwork (vollflächig, mit Scrim) plus einen Track-Fortschrittsbalken (client-seitig zwischen den Polls interpoliert). Transport, Lautstärke, Gerätewahl und die Zeitangaben liegen **ausschließlich** auf Screen 2. Die gesamte Kachel ist tippbar und öffnet Screen 2 — auch im Leerlauf, wo sie „Musik starten“ zeigt. Kein Status-Badge auf dem Homescreen; Fehler erscheinen als Text in der Artist-Zeile.
- Bei laufender Session ist die Spotify-Kachel 250 px hoch (großes Artwork), im Leerlauf 180 px — dort ist sie ein beschrifteter Knopf („Musik starten“) und kein leeres Feld, deshalb darf sie Fläche beanspruchen. Zu flach ließ sie außerdem der Termine-Karte Platz, den diese bei wenigen Terminen nicht füllen kann. Beide Grid-Zeilen sind fixe Werte bzw. `1fr` — **bewusst nicht inhaltsabhängig**, weil `renderCalendar` sein Zeilenbudget aus `clientHeight` einer bereits geleerten Liste zieht und eine `fit-content`-Zeile dabei kollabiert.
- Zustand der rechten Spalte läuft über vererbte Custom Properties (`--tile-h`, `--tile-title`) auf `.cards-column`, Größen über die Kiosk-Media-Query. Nicht mischen: Zustands- und Größenregel auf derselben Eigenschaft bei unterschiedlicher Spezifität war die Ursache des alten 150-px-vs-84-px-Artwork-Bugs.
- Die Hero-Uhr ist per Touch umschaltbar zwischen `flip` (Standard), `lcd`, `pulse`, `qlocktwo`, `qlocktwo-ooe` und `analog`. Start-Face kommt aus `app.watch_face` bzw. `APP_WATCH_FACE`, die Nutzerwahl persistiert im `localStorage` — kein Server-Roundtrip.
- Die QLOCKTWO-Faces und die Analoguhr sind quadratisch und damit höhen-, nicht breitengebunden — eine breitere Spalte bringt ihnen nichts. Bei den Wortuhren entfällt deshalb die Wettervorschau komplett zugunsten eines größeren Rasters (355 px): Eine Wortuhr wird *gelesen*, nicht wie Zeiger überflogen, dort ist Größe direkt Lesbarkeit. Es bleibt der aktuelle Wert. Wer das Wetterfeld anfasst, muss den Rasterdeckel neu ausmessen.

**Schnellmenü (Sheet von unten).** Wisch nach oben oder Tipp auf den Griff am
unteren Rand öffnet ein Sheet mit Timer, Uhr-Stil, Nachtmodus und den beiden
Power-Aktionen. Der Griff (44×4 px, Trefferfläche 120×26 px) ist **kein**
Seitenindikator: Er zeigt eine Kante, die sich hochziehen lässt, und liegt in
der Body-Padding-Zone, damit er auf beiden Screens nichts überdeckt. Power-Aktionen
nur mit 2-s-Halten, nie per Tipp; ohne `APP_ALLOW_SHUTDOWN=true` sind sie
deaktiviert und sagen warum.

**Timer.** Ein laufender Timer ist ein Chip oben rechts im Hero und eine Zeile im
Screensaver — beides tabellarische Ziffern, 1-Hz-Tick nur solange ein Timer
lebt. Der Alarm ist Vollbild, pulsiert per Opacity (billig) und endet von selbst
nach 10 Minuten; er hat Vorrang vor dem Screensaver. Ohne Lautsprecher bleibt er
rein visuell — keinen Ton „vorbereiten“.

**Nacht.** Im Nachtfenster ändern sich nur Farben und die Deko-Gradienten,
nichts bewegt sich. Der Screensaver zeigt dann keine Fotos, sondern eine dunkle
Uhr auf Schwarz. Das ist das Maximum, das Software auf diesem Panel kann — das
Backlight hängt an der Display-Platine, nicht am Pi.

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
- Zielansicht immer auch in `1024x600` prüfen

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
