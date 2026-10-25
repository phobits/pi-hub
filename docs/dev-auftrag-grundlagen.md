# Dev-Auftrag: Verlässliche Produktbasis (G-01 bis G-04)

Stand: 25. Oktober 2026. Grundlage: [Produktbacklog](produktbacklog.md) und [Produktanalyse](produktanalyse.md).

## Ziel und Rahmen

Setze die vier hoch priorisierten Grundlagen um und liefere nachvollziehbare Prüfergebnisse. Der Homescreen muss auch bei Ausfällen verständlich bleiben: Welche Information ist verlässlich, welche ist alt und welche wurde noch nie erfolgreich geladen?

Lies vor Beginn `AGENTS.md` und prüfe den aktuellen Code. Die Analyse beschreibt einen früher geprüften Stand; bereits behobene Punkte nicht erneut implementieren. Bestehende, fremde Änderungen erhalten. Dieser Auftrag umfasst keine Features F-01 bis F-12 und keine grundlegende Neugestaltung.

Zielgerät: Raspberry Pi Zero 2 W, 512 MB RAM, 7-Zoll-Touchpanel mit 1024×600 Pixeln, dauerhafter Kioskbetrieb. Architektur bleibt Python/Flask/Waitress und servergerendertes HTML mit Vanilla-JavaScript. Keine neuen Frameworks, zusätzlichen Screens oder Status-Badges. Hinweise alltagssprachlich, mit echten Umlauten, ruhig und platzsparend. Last known good, lokale Fotos und unabhängige Provider bleiben nutzbar.

## G-01 — Lokale State-Abfrage absichern

**Priorität: hoch.** Wenn der Browser den lokalen Dienst nicht erreicht, kann er auch dessen Provider-Fehlerzustände nicht mehr empfangen. Eine weiterlaufende Uhr darf eingefrorene Daten nicht unbemerkt aktuell erscheinen lassen.

### Umsetzung

- Prüfe `fetchState`, `schedulePolling` und die Screensaver-Pollingregeln in `smart_display/web/static/js/app.js` sowie die State-Route in `smart_display/web/routes.py`.
- Begrenze die Laufzeit einer State-Anfrage. Ein hängender Request darf weitere Versuche nicht dauerhaft verhindern. Keine überlappenden Request-Ketten oder zusätzlichen dauerhaften Polling-Schleifen einführen.
- Merke im Browser den letzten erfolgreichen lokalen Kontakt. Eine gültige 200-Antwort und eine 304-Antwort zählen als Erfolg; HTTP-Fehler, Netzwerkfehler, Timeout und ungültige 200-Payloads nicht.
- Zeige nach mehreren verpassten erwarteten Aktualisierungen einen dezenten Hinweis, etwa „Anzeige wird aktualisiert“. Benutze vorhandenen Platz; Kollisionen mit Providerhinweisen und Timerchip ausdrücklich lösen.
- Behandle bewusst pausiertes Polling im Screensaver nicht als Störung. Beim Wiederaufnehmen zeitnah aktualisieren und einen tatsächlichen Ausfall wieder erkennen.
- Gute Inhalte stehen lassen. Nach erfolgreicher Wiederverbindung den lokalen Hinweis selbstständig entfernen. Providerfehler werden dadurch nicht automatisch als behoben markiert.
- Request-Limit, Hinweisschwelle und Wiederholungsintervall dokumentieren. Verstrichene Laufzeiten möglichst monoton messen; Systemzeitkorrekturen dürfen keinen falschen Ausfall auslösen.

### Abnahme

1. Lokalen Dienst stoppen: Gute Daten bleiben sichtbar, nach der festgelegten Frist erscheint der Hinweis.
2. Anfrage dauerhaft offen lassen: Timeout greift und ein späterer Versuch findet statt.
3. HTTP-Fehler und ungültiges JSON beziehungsweise ungültige Payload: kein Absturz, kein falscher Erfolg.
4. Dienst wieder starten: Aktualisierung kehrt ohne Reload zurück, lokaler Hinweis verschwindet.
5. Wiederholte 304-Antworten und längerer Screensaverbetrieb erzeugen keinen Fehlalarm.
6. Wiederverbindung mit weiterhin fehlerhaftem Wetterprovider entfernt nur den lokalen Verbindungsfehler.

## G-02 — Leere und nie geladene Daten unterscheiden

**Priorität: hoch.** „Keine Termine“ darf nur bedeuten, dass ein Kalender erfolgreich abgefragt wurde und im angezeigten Zeitraum leer ist.

### Umsetzung

- Prüfe insbesondere `smart_display/state_store.py`, `smart_display/models.py`, die Provider und `renderCalendar` in `app.js`.
- Unterscheide fachlich: Quelle nicht eingerichtet/deaktiviert; eingerichtet, aber noch kein erfolgreicher Abruf; Erstabruf fehlgeschlagen; erfolgreich leer; erfolgreich mit Daten; späterer Fehler mit guten Bestandsdaten.
- Wähle dafür die kleinste tragfähige Modelländerung. Neue Statuswerte sind kein Selbstzweck. Konfigurationszustand und Abrufgesundheit dürfen bei Bedarf getrennte Felder sein.
- Definiere verständliche Texte: beispielsweise „Kalender nicht eingerichtet“, „Termine werden geladen“ und „Termine nicht verfügbar“. Einrichtungsdetails gehören in die Betreiberhilfe, nicht als technische Warnung auf den Homescreen.
- Fehler eines ersten Abrufs dürfen nicht dauerhaft hinter einem unauffälligen Leerzustand verschwinden. Eine absichtlich deaktivierte Quelle darf umgekehrt keinen Fehleralarm erzeugen.
- Bereits erfolgreich geladene Termine bei späterem Fehler erhalten. Eine erfolgreiche leere Antwort darf alte Termine dagegen regulär ablösen.
- Persistierte alte Snapshots und Schema-Kompatibilität berücksichtigen. Fehlende neue Felder dürfen den Start nicht verhindern und keinen früheren Erfolg erfinden.
- Entsprechende Unterscheidungen bei Wetter und Spotify auf Konsistenz prüfen, ohne die festgelegten vier Formen der Musikleiste unnötig umzubauen.

### Abnahme

Eine Testmatrix deckt mindestens ab: deaktivierte Quelle, fehlende Konfiguration, laufender Erstabruf, falsche Zugangsdaten ohne Cache, erfolgreicher leerer Kalender, gefüllter Kalender, späterer Fehler mit Cache sowie Wiederkehr mit leerem Ergebnis. Prüfe Neustart mit altem Cache. Nur gesichert leere Kalenderdaten zeigen „Keine Termine“; kein Zustand destabilisiert unabhängige Inhalte.

## G-03 — Alter und Ursache verständlich anzeigen

**Priorität: hoch.** „Stand 15:10“ reicht bei mehrtägigen Ausfällen nicht; ein einzelner Providerfehler beweist keine unterbrochene Netzwerkverbindung.

### Umsetzung

- Prüfe `formatStamp`, `renderStatusRail`, `renderWeather`, die Spotify-Fehlerdarstellung und den Screensaver in `app.js`.
- Nutze für das Datenalter weiterhin `snapshot.updated_at`: den Zeitpunkt der letzten guten Abfrage. Weder Fehlerzeitpunkt noch letzter Browserkontakt ersetzen ihn.
- Zeige bei `stale`/`error` am selben lokalen Kalendertag „Stand HH:MM“, am Vortag etwa „Stand gestern HH:MM“, bei älteren Daten ein kurzes eindeutiges Datum mit Uhrzeit. Bei Jahreswechsel das Jahr ergänzen, wenn sonst Mehrdeutigkeit entsteht.
- Berechne Tagesgrenzen in der konfigurierten Display-Zeitzone. Fehlende oder ungültige Zeitstempel dürfen keinen scheinbar korrekten Zeitpunkt erzeugen.
- Die Altersbeschriftung muss auch ohne neue Providerdaten nach Mitternacht wechseln. Nutze vorhandene Minuten-/Tageswechselpfade statt eines zusätzlichen schnellen Timers.
- Vermeide „Offline“ als pauschale Diagnose bei einzelnen Dienstausfällen. Benenne den betroffenen Inhalt alltagssprachlich. Interne Token-, HTTP- oder Providerdetails bleiben aus dem Hauptbild.
- Wetterhinweise gehören ans Wetter, Kalenderhinweise in die reservierte Statusleiste, Spotifyhinweise in die Musikdarstellung. Mit G-01 eine klare Vorrangregel für begrenzten Platz festlegen.
- Veraltetes Wetter im Foto-Screensaver ebenfalls kennzeichnen oder dort weglassen. Eine ausgefallene Quelle darf keine scheinbar aktuelle Aussage hinterlassen.
- `empty` allein löst keinen Veraltet-Hinweis aus. Nicht eingerichtete Quellen nach G-02 behandeln.

### Abnahme

Prüfe gültige Snapshots von heute, gestern, vor 48 Stunden und aus dem Vorjahr sowie fehlende/ungültige Zeitstempel. Prüfe Mitternacht, Jahreswechsel und Sommerzeitwechsel in `Europe/Vienna`. Ein einzelner Kalenderfehler erklärt nicht das ganze Gerät für offline. Home, Spotify und Foto-Screensaver bleiben bei langen Hinweisen, gleichzeitigem Timer und lokaler Verbindungsstörung auf 1024×600 lesbar.

## G-04 — Zielgerät-Abnahme und mindestens 72 Stunden Dauerlauf

**Priorität: hoch.** Funktionierende Desktop-Tests beweisen weder ausreichende Speicherreserve noch dauerhafte Nutzbarkeit auf dem Pi.

### Vorbereitung und Messplan

- Nutze `scripts/measure_pi.py` und vorhandene Betriebswerkzeuge. Erweitere sie nur, wenn für die folgenden Nachweise Daten fehlen.
- Dokumentiere vor dem Lauf: Commit beziehungsweise geprüften Arbeitsstand, Gerät, Betriebssystem, Browser/WPE-Version, relevante Konfiguration ohne Secrets, Messintervalle und Ausgangswerte.
- Lege vorab begründete, numerische Grenzen für minimalen freien Speicher, tolerierten Speichertrend, CPU-Last, State-Latenz und Touchreaktion fest. Unterscheide kalten Start, Aufwärmphase und stabilen Betrieb. Keine nachträgliche Anpassung nur zum Bestehen.
- Erfasse Backend und Browser einschließlich Hilfsprozessen, Swap, SD-Schreiblast, Dienstneustarts, Temperatur und gegebenenfalls Drosselung. Vergleiche gleichartige Betriebszustände; wachsende Foto-Caches nicht mit einem Prozessleck verwechseln.

### Testablauf

Mindestens 72 Stunden auf dem tatsächlichen Zielgerät mit normaler Foto-Rotation, aktiver/pausierter Musik, Wechsel zwischen beiden Screens, Uhrstilen, Timern, Nachtfenstern und Rückkehr zum Tag. Zusätzlich kontrollierte Szenarien dokumentieren:

1. Netzunterbrechung und Wiederkehr: lokale Uhr, Timer und gecachte Fotos bleiben brauchbar; Datenalter stimmt.
2. Einzelner Providerfehler beziehungsweise ungültiger Testzugang: übrige Inhalte bleiben stabil. Keine echten Secrets in Logs oder Bericht schreiben.
3. Lokaler Backend-Ausfall und Wiederanlauf: G-01 ist am Panel sichtbar und erholt sich.
4. Kontrollierter Dienst-/Geräteneustart: Cache, Timer und gewählte Uhrdarstellung verhalten sich wie vorgesehen; unsynchronisierte Systemzeit wird berücksichtigt.
5. Ausfall oder Fehlen der Fotoquelle: gecachte Bilder beziehungsweise Uhr-Fallback bleiben nutzbar.

### Abnahme am Panel

- Kein OOM, kein ungeklärter Neustart und kein fortschreitender, nicht erklärter Speicheranstieg nach der Aufwärmphase.
- Vorab gesetzte Betriebsgrenzen werden eingehalten; absichtlich ausgelöste Neustarts sind getrennt von Fehlern ausgewiesen.
- Uhr, aktuelles Wetter und nächster Termin sind aus 1–2 m bei Tages- und Abendlicht erfassbar.
- Seitenwisch, Schnellmenü, Slider und Screensaver-Aufwecken funktionieren auf dem tatsächlichen Touchpanel.
- Nachtmodus wirkt auf dem vorhandenen Panel angemessen. Dokumentiere die Backlight-Grenze; schwarze Pixel nicht als ausgeschaltetes Display bewerten.
- Nach Fehlern ist kein manueller Browser-Reload zur normalen Nutzung nötig.

Ohne verfügbaren Pi oder ohne abgeschlossenen 72-Stunden-Lauf bleibt G-04 ausdrücklich offen. Liefere dann bereits mögliche lokale Nachweise und einen direkt ausführbaren Geräte-Testplan. Ergebnisse nicht simulieren und Screenshots nicht als bestandene Geräteabnahme ausgeben.

## Prüfungen und Lieferumfang

1. G-01 bis G-03 implementieren, mit gezielten Tests für Zustandsübergänge, Zeitgrenzen und Wiederverbindung. Keine echten externen Konten für automatisierte Tests voraussetzen.
2. Repository-Checks ausführen:

   ```bash
   python3 -m compileall smart_display tests
   python3 -m unittest discover -s tests
   ```

3. Gegen den Demo-Server die betroffenen Zustände visuell prüfen:

   ```bash
   python3 -m smart_display.local_server
   python3 scripts/screenshot-states.py OUT_DIR
   ```

   Screenshots für lokale Verbindungsstörung, Erstabruf ohne gute Daten und mehrtägig veraltete Daten ergänzen, falls die bisherigen Fixtures diese Fälle nicht abdecken. Alle sechs Uhrstile und beide Wetterhöhen berücksichtigen. Bei geänderter Gestenlogik zusätzlich `python3 scripts/check-gestures.py` ausführen.

4. G-04 nach dem beschriebenen Plan durchführen. Ein 72-Stunden-Lauf ersetzt die gezielten Ausfalltests nicht.
5. Einen kurzen Prüfbericht unter `docs/` ablegen: Änderungen, gewählte Schwellen, Ergebnisse je G-ID, reproduzierbare Schritte, Messartefakte und offene Punkte. Screenshots und Messdaten ohne Tokens oder private Kalenderinhalte bereitstellen.
6. Abschließend Git-Diff und Status prüfen. Eigene Änderungen klar benennen; fremde Änderungen weder entfernen noch als eigene Arbeit ausgeben.

**Fertig ist der Gesamtauftrag erst, wenn G-01 bis G-03 belegt funktionieren und G-04 auf dem Zielgerät bestanden ist.** Falls nur lokale Umsetzung möglich war, genau diesen Teilabschluss und die fehlenden Gerätenachweise nennen.
