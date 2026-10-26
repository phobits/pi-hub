# Wetter-Fixtures

Echte, kleine Antworten der Provider, aufgenommen am 2026-10-26 um ~09:22 UTC
(Wien 48.2082, 16.3738 — die Default-Koordinaten aus `config/default.yaml`;
Linz 48.31, 14.29, weil dort gerade Regen lag). Genau die Parameter, die die
Provider anfragen. Nicht von Hand bearbeiten — Varianten (Nullwerte,
Teilantworten, mehrere Warnungen) erzeugen die Tests aus diesen Dateien.

| Datei | Quelle |
|---|---|
| `claef_vienna.json` | GeoSphere `nwp-v2-1h-1km`, Lauf 03:00 UTC, ab laufender Stunde bis Lauf + 60 h |
| `claef_bad_param.json`, `claef_outside.json` | dieselbe API, HTTP 400 (unbekannter Parameter / Punkt außerhalb) |
| `inca_linz_rain.json`, `inca_vienna_dry.json` | GeoSphere `nowcast-v1-15min-1km`, `rr` je 15 min |
| `warnings_vienna_none.json` | Warn-API `getWarningsForCoords`, bestätigt ohne Warnung |
| `warnings_outside.json` | dieselbe, HTTP 404 (Berlin, keine österreichische Gemeinde) |
| `warnings_documented_example.json` | Beispielantwort aus der offiziellen OpenAPI (`openapi.hub.geosphere.at/warnapi/v1/openapi.json`) — am Aufnahmetag gab es in ganz Österreich keine Warnung |
| `openmeteo_vienna.json` | Open-Meteo `/v1/forecast`, 7 Tage, `timeformat=unixtime` |
| `openmeteo_dst_window.json` | dasselbe für 24.–30.10.2026, enthält den 25-Stunden-Tag 25.10. |
