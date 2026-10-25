# Produktanalyse: Smart Display für den Alltag

Stand: 25. Oktober 2026. Perspektive: Senior Product Development für Smart Displays, mit dem Produktmaßstab eines Google-Nest-Hub-erfahrenen Teams. Dies ist eine analytische Rollenperspektive, keine Behauptung eigener Beschäftigung bei Google und kein aktueller Funktionsvergleich mit Nest Hub.

Bewertet wird ein persönliches, dauerhaft laufendes Raumdisplay auf Raspberry Pi Zero 2 W mit 512 MB RAM und einem 7-Zoll-Touchpanel mit 1024×600 Pixeln. Sein Kernauftrag lautet: Uhrzeit, Wetter und nächsten Termin ohne Bedienung erfassen; Musik und Timer mit wenigen Berührungen steuern; im Hintergrund angenehm bleiben. Ein Sprachassistent, App-Store oder allgemeines Smart-Home-Dashboard ist dafür keine Voraussetzung.

Grundlage sind README und AGENTS.md, die Implementierung in `smart_display/`, die vorhandenen Tests und die bestehenden 1024×600-Aufnahmen unter `docs/konzept/homescreen-v2/screens/umsetzung/` (Homescreen mit Timer, Offline-Zustand, Spotify und Schnellmenü). Die Aufnahmen sind visuelle Referenzen, kein Nachweis eines heute durchgeführten Gerätetests. Lesbarkeit aus zwei Metern, Touchqualität, tatsächliche Nachtwirkung und Dauerlaufstabilität bleiben am realen Panel zu validieren.

## Stärken

### Ein klarer Auftrag statt einer Sammlung von Widgets

Die linke 612-Pixel-Spalte priorisiert Uhr und Wetter; rechts stehen Termine und Musik. Das passt zum Gebrauch im Vorbeigehen: Die häufigste Frage bekommt die größte Darstellung. Die Referenzaufnahmen zeigen eine ruhige Hierarchie mit wenigen Flächen und deutlicher Uhr. Dass Termine keine gleichberechtigte große Kartenwand erhalten, ist auf sieben Zoll eine gute Produktentscheidung.

### Anzeige und aktive Bedienung sind sinnvoll getrennt

Der Homescreen bleibt übersichtlich; Cover, Scrubbing, Transport, Lautstärke, Geräte und Playlists liegen gemeinsam auf dem zweiten Screen. Die ganze Musikleiste ist der Einstieg. Die Restzeit unterstützt eine konkrete Alltagsfrage: Wie lange läuft dieser Titel noch? Vier Folgetitel schaffen Orientierung ohne eine endlose Musikbibliothek auf dem Raumdisplay.

### Der Kalender priorisiert den nächsten Handlungsbedarf

Der nächste laufende oder kommende zeitgebundene Termin erhält mehr Gewicht und eine relative Zeit. Einzeilige Titel und ein höhenabhängiges Zeilenbudget schützen die Informationsdichte. Vergangene Termine werden reduziert und abgeblendet; bei knapper Fläche werden spätere Abschnitte zuerst gekürzt. Das ist eine brauchbare Agenda für einen Blick, kein verkleinerter Desktop-Kalender (`calendar_layout.py`, `renderCalendar` und `refreshCalendarMinute` in `app.js`).

### Fehlerfestigkeit gehört zur Architektur

Provider arbeiten serverseitig; ein fehlgeschlagener Abruf verwirft nicht sofort die letzte gute Information. Der persistierte Zustand, lokale Fotos und die Uhr als Foto-Fallback erhalten den Nutzen bei Netz- und Quellausfällen. Cache-Quarantäne bei beschädigten Daten verhindert einen dauerhaft kaputten Start. Fehlertexte bleiben überwiegend alltagssprachlich. Das ist für ein unbeaufsichtigtes Haushaltsgerät wichtiger als zusätzliche Integrationen (`state_store.py`, Cache-Module, README).

### Hardwaregrenzen prägen die Umsetzung

Flask, Waitress, servergerendertes HTML und Vanilla-JavaScript passen zum knappen Budget. Sichtbarkeitsabhängige Uhranimationen und Queue-Abfragen, ETags sowie gedrosselte Persistenz sparen laufende Arbeit und SD-Schreibzugriffe. Watchdog, systemd-Betrieb und das Messskript behandeln das Produkt als Gerät. Das sind gute Voraussetzungen; tatsächliche Ressourcenreserven sind damit noch nicht nachgewiesen.

### Ambient-Modus und Sicherheitsdetails sind durchdacht

Fotos mit Uhrinformation geben dem Display auch ohne aktive Aufgabe einen Platz im Raum. Nachtfarben und die dunkle Uhr reduzieren visuelle Unruhe innerhalb der Panelgrenzen. Timer überleben Neustarts und verdrängen den Screensaver bei Ablauf. Ausschalten und Neustart benötigen bewusstes Halten und sind standardmäßig gesperrt. Die sechs lokal gespeicherten Uhrstile erlauben persönliche Gestaltung ohne Serverlast.

## Lücken

### Vertrauenslücke zwischen Browser und lokalem Dienst

`fetchState` lässt bei HTTP-Fehlern oder Verbindungsabbruch den vorhandenen Zustand stehen und protokolliert nur in der Konsole. Der Browser erhält dann auch keine neuen Provider-Fehlerzustände. Eine weiterhin laufende Uhr kann deshalb neben unbemerkt eingefrorenen Terminen und Wetterdaten stehen. Außerdem wartet das Polling auf den Abschluss des Requests; ohne explizites Zeitlimit kann eine hängende Anfrage weitere Aktualisierungen verzögern. Last known good ist richtig, braucht aber eine vom Backend unabhängige Aussage zur letzten erfolgreichen lokalen Aktualisierung.

### Leere Daten sind nicht immer verlässlich interpretierbar

Bei fehlenden Kalenderabschnitten zeigt `renderCalendar` „Keine Termine“. Der Untertext unterscheidet teilweise nach Snapshot, die Hauptaussage aber nicht zwischen eingerichtetem leerem Kalender und fehlender Einrichtung. `StateStore.mark_error` hält einen vorherigen `empty`-Zustand auch bei Fehlern auf `empty`. Das vermeidet Alarmismus, kann aber einen fehlgeschlagenen Erstabruf verbergen. „Nichts geplant“ darf nicht dieselbe Bedeutung bekommen wie „noch nichts abrufbar“.

### Zeitstempel verlieren bei längeren Ausfällen ihre Aussagekraft

`formatStamp` zeigt nur HH:MM. Ein mehrere Tage alter Snapshot kann dadurch aktuell wirken. Die Statusleiste fasst Wetter und Kalender zusammen und nennt auch einzelne Providerprobleme „Offline“. Das ist eine stärkere Diagnose, als aus einem fehlgeschlagenen Abruf folgt: Auch ein Token oder Dienst kann die Ursache sein. Im Foto-Screensaver wird Wettertext übernommen, ohne dort dieselbe Alterskennzeichnung auszugeben (`renderWeather`).

### Bedienbarkeit setzt gelerntes Verhalten voraus

Der Rückweg aus Spotify ist ausschließlich ein Wisch; die Uhr wechselt beim Antippen ihren Stil. Auf Fotos weckt ein Tipp auf die Uhr oder ein Doppeltipp, nachts dagegen jeder Tipp. Diese bewussten Regeln verhindern Gestenkonflikte, sind aber für Gäste kaum selbst erklärend. Der Griff hilft beim Schnellmenü, erklärt jedoch die übrigen Gesten nicht. Ein dauerhaft sichtbares Navigationssystem wäre dafür unverhältnismäßig.

### Kalenderdarstellung kann Vollständigkeit und Interaktion suggerieren

Das Zeilenbudget kürzt Termine ohne sichtbaren Hinweis auf die Anzahl ausgeblendeter Einträge. Lange Titel enden mit Ellipse, ohne Detailansicht. Der hervorgehobene Termin wird als `li` ohne Klickaktion erzeugt, obwohl seine Fläche wie eine bedienbare Einheit wirkt (`createCalendarNextRow`). Das ist eine konkrete Spannung zum Produktprinzip „nur Antippbares bekommt eine Fläche“.

### Timer sind nützlich, aber nur für einen begrenzten Einsatz

Das Schnellmenü bietet sechs feste Dauern. Der Dienst unterstützt individuelle Dauer und Beschriftung, die UI sendet beim Erstellen jedoch nur die gewählte Minutenzahl. Drei parallele Timer lassen sich damit schlechter nach Aufgabe unterscheiden. Der rein visuelle Alarm ist für die Hardware korrekt, kann aber außerhalb der Blickrichtung übersehen werden. Das ist eine Einsatzgrenze, die sich nicht mit Softwarelautstärke lösen lässt.

### Einrichtung und Privatheit bleiben Betreiberaufgaben

Standort, Kalender, Spotify-Zugang und Fotoquelle werden über Konfiguration und Hilfsskripte eingerichtet. Für den Entwicklerhaushalt ist das praktikabel; für eine Weitergabe an andere Haushalte fehlt ein geführter Erstbetrieb samt verständlichem Wiederverbinden. Sichtbare Termine, Cover und Fotos haben zudem keinen angebotenen Gästemodus. Auf einem gemeinsam sichtbaren Raumdisplay ist das eine Produktfrage, auch wenn keine Mehrbenutzerplattform benötigt wird.

### Nachweise fehlen dort, wo der Raum entscheidet

Die Test- und Screenshot-Werkzeuge sind vorhanden. Die hier geprüften Quellen belegen jedoch keine aktuelle Abnahme über mehrere Tage auf dem Zielgerät. Kleine Sekundärtexte, gedimmte vergangene Termine, Slider und Nachtfarben müssen unter realer Beleuchtung und mit dem tatsächlichen WPE-Browser bestehen. Eine schwarze Darstellung ersetzt keine Backlight-Steuerung; zusätzliche CSS-Abdunklung kann diese Hardwaregrenze nicht beheben.

## Verbesserungen

Priorität **hoch** schützt richtige Entscheidungen und verlässlichen Dauerbetrieb. **mittel** verbessert häufige Alltagsabläufe. **niedrig** erweitert Komfort, nachdem der Kern nachweisbar trägt. Die folgenden Punkte sind Produktvorschläge, keine bereits implementierten Funktionen.

| Verbesserung | Priorität | Begründung und schlanke Umsetzung | Abnahmekriterium |
|---|---|---|---|
| Lokale Aktualisierung überwachen | hoch | Im Browser den letzten erfolgreichen State-Abruf merken, Requests begrenzen und nach mehreren verpassten Abrufen einen ruhigen Hinweis wie „Anzeige wird aktualisiert · Stand …“ zeigen. Daten stehen lassen; erfolgreiche 304-Antworten als Kontakt werten. So bleibt ein Backend-Ausfall erkennbar, auch wenn kein neuer Snapshot ankommt. | Dienst stoppen sowie Anfrage hängen lassen: Anzeige bleibt bedienbar, Hinweis erscheint nach festgelegter Frist, Polling erholt sich und Hinweis verschwindet nach Wiederkehr. |
| Leeren Kalender und fehlenden Abruf unterscheiden | hoch | Serverseitig „nicht eingerichtet“, „noch nie erfolgreich geladen“ und „erfolgreich leer“ unterscheiden. Nur der letzte Fall darf „Keine Termine“ als gesicherte Aussage zeigen. Hinweise zur Einrichtung gehören ins Schnellmenü oder in eine Betreiberansicht, nicht als technische Badges auf den Homescreen. | Ohne Konfiguration, mit falschen Zugangsdaten und mit tatsächlich leerem Kalender entstehen unterscheidbare, verständliche Zustände; bestehende gute Termine bleiben bei Fehlern erhalten. |
| Datenalter eindeutig und am betroffenen Inhalt zeigen | hoch | Am selben Tag HH:MM beibehalten, bei älteren Daten etwa „Stand gestern 15:10“ beziehungsweise ein kurzes Datum zeigen. Einzelne Dienstausfälle nicht pauschal als Netzdiagnose behandeln. Im Screensaver veraltetes Wetter kennzeichnen oder weglassen. | Ein 48 Stunden alter Snapshot ist auf Home und Screensaver eindeutig alt; ein Kalenderfehler erklärt nicht unbegründet das gesamte Gerät für offline. |
| Zielgerät als verbindliche Produktabnahme nutzen | hoch | Mit `measure_pi.py` eine Basis und einen mindestens 72-stündigen Lauf mit Fotos, Musik, Nachtwechsel und Netzunterbrechung dokumentieren. Speicherreserve, Verlauf, Reaktionszeit und Wiederanlauf am Panel prüfen; Grenzwerte vor der Messung festlegen. Das reduziert das größte Risiko eines dauerhaft laufenden 512-MB-Geräts. | Kein OOM und kein ungeklärter Neustart, kein dauerhaft wachsender Speicherverbrauch; Ausfälle und Rückkehr dokumentiert. Kerninformationen aus 1–2 m und Touchaktionen am realen Panel geprüft. |
| Kalenderdetails bei Bedarf zugänglich machen | mittel | Den hervorgehobenen Termin tatsächlich antippbar machen: ein einfaches Overlay mit vollständigem Titel und den bereits verfügbaren Angaben. Ausgeblendete Einträge mit einem knappen „+N weitere“ zugänglich machen; dessen Platz im Zeilenbudget berücksichtigen. Keine zweite Kalender-App und keine zweizeiligen Home-Titel. | Lange Titel und gekürzte Tage lassen sich vollständig lesen; Schließen führt ohne Positionsverlust zurück, auch bei alten Daten. |
| Gesten einmalig erklären | mittel | Beim ersten Gebrauch kurze, wegklickbare Hinweise für Rückwisch, Schnellmenü und Aufwecken geben; eine kompakte Hilfe erneut im Sheet anbieten. Keine Seitenpunkte, kein Startknopf und keine automatische Demoanimation. | Neue Nutzer können Musik öffnen, zurückkehren und das Foto verlassen, ohne Anleitung durch den Betreiber; Hinweise erscheinen nach Erledigung nicht ständig erneut. |
| Timer für konkrete Aufgaben ergänzen | mittel | Neben Presets eine kompakte individuelle Dauer und optionale kurze Bezeichnung anbieten, vorhandene API nutzen. Beim ersten Timer verständlich erklären, dass der Alarm nur auf dem Display erscheint. Keine Audiofunktion ohne entsprechende Hardware. | 7-Minuten-Timer und drei unterscheidbare Timer sind mit Touch anlegbar; Zustand und Beschriftung bleiben nach Neustart erhalten. |
| Betreiberhilfe für Einrichtung und Wiederverbinden bündeln | mittel | Zunächst eine kurze lokale Checkliste und eine vom Hauptbild getrennte Statusansicht: Quelle eingerichtet, letzter Erfolg, nächster sinnvoller Schritt. Zugangsdaten weiter serverseitig halten. Einen vollständigen Einrichtungsassistenten erst bei geplanter Weitergabe bauen. | Ein Betreiber kann falschen Kalenderzugang, fehlendes Spotify-Gerät und ausgefallene Fotoquelle unterscheiden und beheben, ohne Frontend-Konsole oder Geheimnisse auf dem Display. |
| Diskreten Gästemodus anbieten | mittel | Ein Schalter im Sheet verbirgt private Kalenderinhalte und persönliche Fotos zugunsten der vorhandenen Uhransicht. Umfang und Rückkehrregel klar anzeigen; serverseitiges Abrufen muss dafür nicht umgebaut werden. | Gastmodus bleibt auch nach Screensaver-Wechsel konsistent und zeigt keine privaten Vorschauen; Ausschalten stellt den vorherigen Modus wieder her. |
| Wetter um eine handlungsrelevante Aussage erweitern | niedrig | Erst nach Nutzertest optional eine kurze Regenaussage ergänzen, statt weitere Wetterkarten einzuführen. Aktuell werden keine stündlichen Niederschlagsdaten abgefragt; Nutzen muss zusätzlichen Abruf und Platz rechtfertigen. | Die Frage „Brauche ich gleich einen Schirm?“ wird im Test schneller beantwortet; Ausfall der Zusatzdaten verändert weder Temperatur noch Drei-Tage-Vorschau. |

Die erste Ausbaustufe sollte die vier hoch priorisierten Punkte abschließen. Danach lohnt ein kleiner Nutzungstest im Haushalt: Uhrzeit und Wetter im Vorbeigehen erkennen, einen langen Termin lesen, Musik steuern, einen Timer setzen und nach einem Netzausfall den Datenstand richtig einschätzen. Gemessen werden Aufgabenerfolg, Fehlberührungen und benötigte Hilfe. Neue Integrationen sind erst dann sinnvoll, wenn sie einen beobachteten Alltagsbedarf lösen und das Gerät dabei ruhig und zuverlässig bleibt.
