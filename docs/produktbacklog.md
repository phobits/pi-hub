# Produktbacklog: Die nächsten sinnvollen Funktionen

Stand: 25. Oktober 2026. Ergänzung zur [Produktanalyse](produktanalyse.md).

Der Backlog beschreibt Vorschläge, keine zugesagten oder bereits implementierten Funktionen. Maßstab ist ein ruhiges Raumdisplay auf Pi Zero 2 W, 512 MB RAM und 1024×600 Pixeln. Priorisiert wird der Nutzen im Haushalt: schneller orientieren, weniger zum Telefon greifen, wiederkehrende Handgriffe vereinfachen. Die Reihenfolge ist eine Produkthypothese; ein kurzer Haushaltstest soll sie bestätigen.

**Empfehlung:** Zuerst Kalenderdetails und alltagstaugliche Timer ergänzen. Danach Regenausblick, Abfuhrtermine und Musikfavoriten erproben. Diese Funktionen erweitern vorhandene Aufgaben, ohne aus dem Display eine allgemeine App-Plattform zu machen.

## Priorisierung und Umfang

- **hoch:** häufiger Nutzen im Kernablauf oder Voraussetzung für verlässliche Nutzung.
- **mittel:** klarer Nutzen für einen passenden Haushalt; nach Bedarf aktivieren.
- **niedrig:** erst nach belegtem Bedarf oder mit zusätzlicher Hardware umsetzen.

Aufwand ist relativ: **S** = begrenzte Erweiterung vorhandener Daten und UI, **M** = zusätzliche Zustandslogik oder Provider-Erweiterung, **L** = neue Integration oder deutlicher Betriebsaufwand. Das sind keine Zeitschätzungen. Die tatsächliche Größe muss vor Umsetzung am dann aktuellen Code geprüft werden.

Die zwei Screens bleiben bestehen. Zusätzliche Bedienung erfolgt in gezielt geöffneten Overlays oder im Schnellmenü; Spotify bleibt vollständig auf Screen 2. Der Homescreen erhält keine neue Kartenwand und keine zusätzlichen Navigationspunkte. Jede sichtbare Ergänzung muss vorhandenen Platz nutzen oder bewusst andere Information ersetzen.

## Voraussetzungen aus der Produktanalyse

Diese Arbeiten sind keine neuen Features, sollten aber vor der ersten Erweiterungsrunde erledigt beziehungsweise als Abnahmeschranke behandelt werden:

| ID | Voraussetzung | Priorität | Fertig, wenn … |
|---|---|---|---|
| G-01 | Ausfall der lokalen State-Abfrage erkennbar machen; Requests zeitlich begrenzen | hoch | alte Daten nutzbar bleiben, ein Browserhinweis nach verpassten Abfragen erscheint und die Aktualisierung selbstständig zurückkehrt. |
| G-02 | Nicht eingerichtet, Erstabruf fehlgeschlagen und erfolgreich leer unterscheiden | hoch | insbesondere „Keine Termine“ nur eine verlässlich leere Agenda bezeichnet. |
| G-03 | Mehrtägiges Datenalter und betroffene Quelle verständlich anzeigen | hoch | alte Daten auch im Screensaver nicht als aktuelle Information erscheinen. |
| G-04 | Zielgerät-Abnahme einschließlich Dauerlauf | hoch | eine dokumentierte Basis und ein mindestens 72-stündiger Lauf mit Ausfällen vorliegen; Speicherreserve, Reaktionszeiten und Lesbarkeit am Panel vorab festgelegte Grenzen erfüllen. |

## Feature-Übersicht

„Erweitert Analyse“ greift einen dort bereits genannten Vorschlag auf; „neu“ ergänzt weitere Produktideen. Die Reihenfolge innerhalb einer Priorität bildet die empfohlene Bearbeitungsreihenfolge ab.

| ID | Funktion | Priorität | Aufwand | Herkunft | Ziel |
|---|---|---|---|---|---|
| F-01 | Kalenderdetails und vollständige Tagesagenda | hoch | M | erweitert Analyse | Gekürzte Informationen bei Bedarf lesen |
| F-02 | Individuelle und benannte Timer | hoch | S | erweitert Analyse | Konkrete Koch- und Alltagsaufgaben abdecken |
| F-03 | Regenausblick für die nächsten Stunden | mittel | M | erweitert Analyse | Vor dem Verlassen des Hauses entscheiden |
| F-04 | Abfuhrtermine und wiederkehrende Haushaltstermine | mittel | M | neu | Seltene, leicht vergessene Aufgaben sichtbar machen |
| F-05 | Musikfavoriten und Einschlaf-Stopp | mittel | M | neu | Gewohnte Musik mit wenigen Berührungen starten und beenden |
| F-06 | Rechtzeitig ans Losgehen erinnern | mittel | M | neu | Vom Terminwissen zur konkreten Handlung kommen |
| F-07 | Gästemodus mit klarer Rückkehrregel | mittel | M | erweitert Analyse | Persönliche Inhalte vor Besuch verbergen |
| F-08 | Lokale Einkaufsliste mit Telefonzugang | mittel | L | neu | Spontane Einfälle ohne Tippen am Panel erfassen |
| F-09 | Fotos bewusst pausieren und ausblenden | mittel | M | neu | Mehr Kontrolle über persönliche Ambient-Inhalte |
| F-10 | Hilfe und Einrichtung am Telefon | mittel | L | erweitert Analyse | Bedienung und Betrieb ohne Entwicklerwissen ermöglichen |
| F-11 | Abfahrten für eine feste Haltestelle | niedrig | M | neu | Einen häufig genutzten Arbeitsweg unterstützen |
| F-12 | Raumklima mit lokalem Sensor | niedrig | L | neu | Innenraum statt nur Außenwetter beurteilen |

## Ausgearbeitete Backlog-Einträge

### F-01 — Kalenderdetails und vollständige Tagesagenda

**Nutzeraufgabe:** „Ich erkenne den nächsten Termin, möchte aber den ganzen Titel und weitere Termine des Tages lesen.“

**Begründung:** Die vorhandene Ellipse und das Zeilenbudget sind für den schnellen Blick richtig. Ohne Vertiefung bleibt wichtige Information unerreichbar; die hervorgehobene Fläche verspricht zudem bereits Interaktion.

**Erster Umfang:** Tipp auf den nächsten Termin öffnet ein leichtes Overlay mit vollständigem Titel und Zeit; Ort nur, sofern die Quelle ihn liefert. Ein im Zeilenbudget berücksichtigtes „+N weitere“ öffnet die vollständige Liste des betreffenden Tages. Keine Kalenderbearbeitung, kein Monatsraster. Rendering längerer Listen begrenzen und weitere Einträge bei Bedarf nachladen oder seitenweise zeigen.

**Abnahme:** Lange Titel sind vollständig lesbar; ein voller Tag bleibt mit Touch durchsehbar. Schließen stellt den Homescreen wieder her. Alte Daten behalten ihre Alterskennzeichnung. Listen und Overlay funktionieren ohne Netz mit dem vorhandenen Snapshot.

**Abhängigkeit:** G-02 und G-03; vorhandene Kalenderfelder vor Umsetzung prüfen.

### F-02 — Individuelle und benannte Timer

**Nutzeraufgabe:** „Stelle sieben Minuten für den Tee und zwölf Minuten für die Nudeln.“

**Begründung:** Sechs feste Dauern decken nicht jede Aufgabe ab. Namen machen die bereits unterstützten drei parallelen Timer verständlicher. Die vorhandene API unterstützt Dauer und Beschriftung bereits.

**Erster Umfang:** Im Timerbereich eine zusätzliche Dauerwahl mit großen Plus-/Minus-Tasten und wenigen optionalen Namensvorlagen. Freitext ist nachrangig. Presets bleiben der schnellste Weg; die Obergrenze bleibt bei drei Timern. Einmaliger Hinweis auf den ausschließlich visuellen Alarm.

**Abnahme:** Ein 7-Minuten-Timer ist ohne Bildschirmtastatur anlegbar. Drei benannte Timer sind im Menü unterscheidbar, der Statuschip priorisiert den nächsten Ablauf. Namen und Endzeiten überstehen Neustarts. Ein fehlgeschlagener Start wird nicht als Erfolg dargestellt.

**Abhängigkeit:** G-01; Alarmpriorität und bestehende Screensaver-Logik erhalten.

### F-03 — Regenausblick für die nächsten Stunden

**Nutzeraufgabe:** „Brauche ich für den nächsten Spaziergang einen Schirm?“

**Begründung:** Tageshöchstwerte beantworten diese Entscheidung kaum. Eine kurze zeitbezogene Aussage hat mehr Nutzen als zusätzliche Wetterkennzahlen.

**Erster Umfang:** Vorhandenen Wetterprovider um einen begrenzten stündlichen Niederschlagsausblick erweitern. Eine kurze Aussage in einer bestehenden Wetter-Zweitzeile ersetzt dort bewusst weniger wichtige Information. Kein Radar, keine animierte Karte, kein zusätzlicher Homescreen-Block. Wahrscheinlichkeit und Menge nicht als sichere Vorhersage eines exakten Regenbeginns formulieren.

**Abnahme:** Der relevante Zeitraum ist erkennbar. Bei fehlenden Zusatzdaten bleibt das heutige Wetter unverändert nutzbar. Alte Vorhersagen werden nach ihrer Gültigkeitsgrenze unterdrückt; „kein Regen“ wird nicht aus fehlenden Daten abgeleitet. Beide Wetterhöhen sind visuell geprüft.

**Abhängigkeit:** G-03; konkrete Datenfelder, Nutzungsbedingungen und Abrufbudget vor Implementierung verifizieren.

### F-04 — Abfuhrtermine und wiederkehrende Haushaltstermine

**Nutzeraufgabe:** „Welche Tonne muss ich heute Abend hinausstellen?“

**Begründung:** Solche Aufgaben sind selten genug, um vergessen zu werden, aber regelmäßig genug für nachhaltigen Nutzen. Sie passen besser zum Raumdisplay als Nachrichten oder beliebige Widgets.

**Erster Umfang:** Einen explizit ausgewählten CalDAV-Kalender beziehungsweise eine importierte ICS-Datei für Abfuhr und Haushalt unterstützen. Zunächst keine kommunalen Webseiten scrapen. Ereignisse am Vortag und am betreffenden Tag in der bestehenden Agenda markieren; keine zusätzliche Dauerfläche. Statische ICS-Importe verständlich mit ihrem abgedeckten Zeitraum kennzeichnen.

**Abnahme:** Mehrtägige und ganztägige Ereignisse sowie Wiederholungen über Zeitzonen- und Sommerzeitwechsel stimmen. Ein Import erzeugt keine Duplikate. Verschobene Feiertagstermine stammen aus der Quelle, nicht aus geratenen Regeln. Offline bleibt die letzte gültige Planung sichtbar; abgelaufene Importabdeckung ist erkennbar.

**Abhängigkeit:** F-01 und G-03; vorab prüfen, was der bestehende CalDAV-Provider bereits abdeckt.

### F-05 — Musikfavoriten und Einschlaf-Stopp

**Nutzeraufgabe:** „Starte meine übliche Playlist auf dem Küchengerät und beende die Wiedergabe in 30 Minuten.“

**Begründung:** Wiederholte Geräte- und Playlistwahl ist unnötige Reibung. Ein begrenzter Stopp ergänzt die Nutzung am Abend ohne neue Medienplattform.

**Erster Umfang:** Bis zu vier Favoriten aus Playlist und Zielgerät im vorhandenen Spotify-Overlay speichern. Dazu 15/30/60 Minuten bis zum Pausieren, sichtbar und abbrechbar auf Screen 2. Stoppauftrag serverseitig verwalten; er ist kein visueller Alarmtimer. Vor Ausführung prüfen, ob weiterhin die zugehörige Wiedergabesitzung beziehungsweise das gewählte Ziel gesteuert wird; bei Unsicherheit nicht fremde Wiedergabe pausieren.

**Abnahme:** Ein Favorit startet ohne erneute Auswahl, sofern das Ziel verfügbar ist. Fehlendes Gerät oder ungültiger Zugang führt zu einer verständlichen Meldung ohne Erfolgssimulation. Ein Wechsel des Geräts oder Nutzungskontexts wird berücksichtigt. Überfällige Aufträge nach Neustart pausieren nicht überraschend spätere Musik. Auf Screen 1 entstehen keine neuen Bedienelemente.

**Abhängigkeit:** Spotify-API-Möglichkeiten und Sitzungsabgrenzung prüfen; Einschlaf-Stopp erst freigeben, wenn die Zielzuordnung zuverlässig gelingt.

### F-06 — Rechtzeitig ans Losgehen erinnern

**Nutzeraufgabe:** „Erinnere mich zehn Minuten vor diesem Termin daran, loszugehen.“

**Begründung:** Die relative Terminzeit informiert, fordert aber keine Handlung ein. Eine freiwillige Erinnerung hilft bei ausgewählten Terminen, ohne jeden Termin zum Alarm zu machen.

**Erster Umfang:** In F-01 pro Termin eine lokale visuelle Erinnerung mit festem Vorlauf aktivieren. Keine Routenberechnung und keine automatische Interpretation von Adressen. Ruhiger, bestätigbarer Hinweis; ein klingelnder Timer behält Vorrang.

**Abnahme:** Terminverschiebung und Löschung ändern beziehungsweise entfernen die Erinnerung. Jede Erinnerung erscheint nur einmal, wird nach Neustart nicht doppelt ausgelöst und ist als rein visuell verständlich. Bei nicht synchronisierter Systemzeit wird keine verlässliche Pünktlichkeit behauptet. Abgelaufene Erinnerungen sammeln sich nicht als Meldungsstapel.

**Abhängigkeit:** F-01, stabile Ereignis-IDs und geprüfte Wiederholungslogik; aktuelle Kalenderdaten als Voraussetzung für ein verlässliches Versprechen.

### F-07 — Gästemodus mit klarer Rückkehrregel

**Nutzeraufgabe:** „Besuch soll weder meine Termine noch meine persönlichen Fotos sehen.“

**Begründung:** Das Display ist gemeinsam sichtbar, die Daten sind persönlich. Ein einziger verständlicher Modus ist wertvoller als ein vollständiges Kontensystem.

**Erster Umfang:** Schalter im Sheet; private Termine, Fotos und Musikmetadaten auf beiden Screens, in Overlays und im Screensaver verbergen. Uhr, Wetter und neutrale Timeranzeige bleiben möglich. Bewusstes manuelles Beenden; Zustand über Neustarts erhalten. Es handelt sich um Sichtschutz, nicht um Zugriffsschutz gegen Personen mit Gerätezugang.

**Abnahme:** Aktivieren schließt bereits offene private Ansichten. Kein nächster Foto- oder State-Wechsel blendet Inhalte wieder ein. Timerbeschriftungen werden ebenfalls verborgen. Rückkehr ist am Gerät verständlich und stellt die vorherige Konfiguration wieder her.

**Abhängigkeit:** Einheitliche Inhaltsregeln für alle Ansichten; keine zusätzliche Benutzerverwaltung.

### F-08 — Lokale Einkaufsliste mit Telefonzugang

**Nutzeraufgabe:** „Milch fehlt; ich möchte sie kurz ergänzen und beim Einkauf abhaken.“

**Begründung:** Eine gemeinsame, kleine Liste bietet Haushaltsnutzen. Eine Tastatur auf sieben Zoll würde jedoch die Hauptnutzung stören; Eingabe gehört vorzugsweise aufs Telefon.

**Erster Umfang:** Eine lokale Liste mit Hinzufügen, Abhaken und Löschen. Am Display nur eine aufgerufene Overlay-Ansicht, am Telefon eine schlanke servergerenderte Seite. Zugang bewusst koppeln. Kein Cloudkonto, keine Push-Nachrichten und keine automatische Rezeptplattform.

**Abnahme:** Zwei Geräte überschreiben Änderungen nicht unbemerkt; Neustarts erhalten die Liste. Ohne Internet bleibt sie im lokalen Netz nutzbar. Der Zugriff von außerhalb des Heimnetzes wird nicht versprochen. Ohne Kopplung sind weder Lesen noch Schreiben möglich; Eingaben werden begrenzt und sicher ausgegeben.

**Abhängigkeit:** Bewusste Architekturentscheidung: Der Dienst lauscht derzeit auf Loopback. LAN-Zugang benötigt Authentifizierung, passenden Transport- und Ursprungsschutz sowie einen abgesicherten Bereitstellungsweg. Kein bloßes Umstellen auf `0.0.0.0`. Erst umsetzen, wenn der Haushalt eine lokale statt unterwegs erreichbare Liste tatsächlich nutzen möchte.

### F-09 — Fotos bewusst pausieren und ausblenden

**Nutzeraufgabe:** „Dieses Bild möchte ich länger ansehen; ein anderes soll hier nicht mehr erscheinen.“

**Begründung:** Fotos schaffen emotionalen Wert. Ohne Kontrolle über Auswahl und Tempo kann derselbe Inhalt im Alltag stören.

**Erster Umfang:** Fotoaktionen ausschließlich über einen bewusst geöffneten Bereich im Schnellmenü: aktuelles Foto pausieren/fortsetzen und lokal aus der Rotation ausblenden. Keine Löschung in Lightroom oder der Originalsammlung. Ausgeblendete Bilder lassen sich in einer einfachen Verwaltung wieder zulassen. Bestehende Tipp- und Wischgesten auf dem Foto bleiben erhalten.

**Abnahme:** Ausblendung übersteht Neustarts und Albumsynchronisierung. Verlauf und Rotation überspringen das Bild. Sind alle Bilder ausgeblendet, erscheint die vorhandene Uhr. Pause übersteht keinen unbegrenzten vergessenen Zustand: Beim Verlassen des Screensavers endet sie, und dieser Umfang wird klar kommuniziert.

**Abhängigkeit:** Stabile Fotoidentität über Pfadwechsel hinweg; lokale Ausschlussliste mit begrenzten Schreibzugriffen. Gästemodus respektieren.

### F-10 — Hilfe und Einrichtung am Telefon

**Nutzeraufgabe:** „Ich möchte Standort oder Nachtzeiten ändern und verstehen, warum Musik nicht startet.“

**Begründung:** Konfigurationsdateien sind für den Betreiber brauchbar, aber schwer weiterzugeben. Das Telefon eignet sich besser für Texteingabe und Verbindungsabläufe als das Panel.

**Erster Umfang:** Zuerst lokale Bedienhilfe und eine klare Betreibercheckliste; anschließend optional eine gekoppelte Telefonansicht für Standort, Nachtzeiten und Quellenstatus. Zugangsdaten und Tokens ausschließlich serverseitig verarbeiten und nie zurück ausgeben. Geführtes Spotify-Verbinden als eigene Folgestufe nach Prüfung des unterstützten Authentifizierungsablaufs.

**Abnahme:** Änderungen werden validiert und atomar gespeichert; ein ungültiger Wert zerstört keine funktionierende Konfiguration. Ausfälle sind nach Quelle und nächstem Schritt verständlich. Koppelzugang ist widerrufbar. Ohne Telefon bleiben Uhr und reguläre Bedienung verfügbar. Gestenhilfe führt weder Seitenindikatoren noch einen Startknopf ein.

**Abhängigkeit:** Gleiches bewusst abgesichertes LAN-Konzept wie F-08; beide Funktionen sollen einen gemeinsamen Zugang nutzen. Kein generisches Administrationsdashboard bauen.

### F-11 — Abfahrten für eine feste Haltestelle

**Nutzeraufgabe:** „Lohnt es sich, jetzt zur üblichen Haltestelle zu gehen?“

**Begründung:** Für einen regelmäßig genutzten Arbeitsweg potenziell sehr hilfreich, für andere Haushalte ohne Wert. Deshalb erst nach konkretem Bedarf.

**Erster Umfang:** Eine Haltestelle, eine Richtung, höchstens drei Abfahrten in einer aufgerufenen Ansicht. Datenquelle serverseitig anbinden und cachen. Keine Karte, Reiseplanung oder laufende Standortermittlung. Eine dauerhafte Home-Platzierung bedarf einer gesonderten Entscheidung über die verdrängte Information.

**Abnahme:** Fahrplan und Echtzeit sind unterscheidbar; ausgefallene oder bereits vergangene Abfahrten werden nicht als sichere Empfehlung gezeigt. Alte Minutenwerte zählen nicht scheinbar live weiter. Ein Providerfehler beeinflusst weder Kalender noch Wetter.

**Abhängigkeit:** Passende regionale Quelle, Abdeckung, Nutzungsrechte und Abrufgrenzen erst bei Umsetzung verifizieren.

### F-12 — Raumklima mit lokalem Sensor

**Nutzeraufgabe:** „Wie warm und feucht ist es hier drinnen?“

**Begründung:** Außenwetter beantwortet keine Innenraumfrage. Ein zusätzlicher Sensor kann echten Nutzen bringen, erhöht jedoch Hardware- und Wartungsaufwand.

**Erster Umfang:** Ein lokaler Temperatur-/Feuchtesensor, serverseitige Messung in niedriger Frequenz und ein einzelner kompakter Wert in einer bewusst gewählten Ansicht. Keine Gesundheitsdiagnosen und keine Schimmelprognose aus zwei Messwerten.

**Abnahme:** Innen- und Außentemperatur sind eindeutig beschriftet. Fehlender Sensor, unplausible Werte und alte Messungen sind erkennbar. Abziehen des Sensors blockiert keinen anderen Provider. Montage nahe Pi oder Display verfälscht die Messung nicht systematisch.

**Abhängigkeit:** Hardwareauswahl, Anschluss, Strombedarf und Montage vorher prüfen; ohne beschafften Sensor keine UI mit Platzhalterversprechen ausliefern.

## Empfohlene Ausbaureihenfolge

1. **Verlässliche Basis:** G-01 bis G-04 abschließen. Vorhandene Analysebefunde am dann aktuellen Code erneut prüfen.
2. **Vorhandene Aufgaben vervollständigen:** F-01 und F-02 liefern. Im Haushalt beobachten, ob weniger Titel unlesbar bleiben und die Timer tatsächlich genutzt werden.
3. **Eine neue Alltagshilfe auswählen:** Je nach Haushalt zuerst F-03 oder F-04. F-05 danach, wenn Musik regelmäßig am Display gestartet wird. Nicht alle Integrationen gleichzeitig hinzufügen.
4. **Gezielt vertiefen:** F-06, F-07 und F-09 nach Bedarf; Privatsphäre bei häufigem Besuch vorziehen.
5. **Erweiterung des Betriebsmodells entscheiden:** F-08 und F-10 nur mit ausdrücklich gewünschtem Telefonzugang und gemeinsamem, abgesichertem LAN-Konzept. F-11 und F-12 bleiben bedarfsabhängige Optionen.

## Gemeinsame Fertig-Kriterien

- Neues Verhalten ist auf 1024×600 visuell geprüft, bei geänderten Gesten zusätzlich mit den vorhandenen Gestentests; die Abnahme am realen Panel bleibt erforderlich.
- Kerninformationen bleiben aus 1–2 m lesbar. Neue Trefferflächen und Slider sind am Touchpanel geprüft. Deutsche Texte verwenden echte Umlaute.
- Nicht eingerichtet, erfolgreich leer, veraltet und fehlerhaft sind sinnvoll behandelt. Gute Bestandsdaten und unabhängige Funktionen bleiben nutzbar.
- Provider, Zugangsdaten und Cache bleiben serverseitig. Neue Daten erhalten begrenzte Größe, sinnvolle Aktualisierungsintervalle und eine klare Gültigkeitsgrenze.
- Keine Arbeit für unsichtbare Ansichten ohne fachlichen Grund; keine neue Endlosanimation, kein zusätzliches Framework und kein unbegrenztes DOM-Wachstum.
- Kritische Zustandslogik erhält passende Tests. Laufzeitmessung vor/nach relevanten Änderungen zeigt tragbare CPU-, Speicher- und Schreiblast auf dem Pi.

## Bewusst zurückgestellt

Sprachassistent, Videostreams, animierte Wetterkarten, Nachrichtenfeed, allgemeine Smart-Home-Steuerzentrale und weitere Uhrstile sind derzeit keine überzeugenden nächsten Schritte. Sie beanspruchen Aufmerksamkeit, Hardware oder Integrationspflege, ohne den Kernauftrag so direkt zu verbessern wie Kalenderdetails, Timer und gezielte Alltagshinweise. Ein Wecker mit zugesicherter Weckwirkung sowie automatische Raumhelligkeit benötigen zunächst geeignete Audio- beziehungsweise Sensor- und Backlight-Hardware. Das aktuelle Panel kann durch Software allein nicht vollständig dunkel geschaltet werden.
