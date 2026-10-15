#!/usr/bin/env bash
# test-panel-blank.sh — Hardware-Test (Ticket #2): wird das Zero-DISP-7A dunkel,
# wenn der Pi das HDMI-Signal abschaltet (DRM/DPMS off)?
#
# Hintergrund: Das Panel steuert sein Backlight nur über OSD bzw. Widerstandsmod;
# über HDMI gibt es keinen Software-Pfad. Viele Scaler schalten das Backlight
# aber selbst ab, sobald kein Signal mehr anliegt — ob dieses Kit das tut,
# beantwortet nur ein Blick aufs Gerät. Dieses Skript macht genau diesen Test
# reproduzierbar. Cog hält den DRM-Master, deshalb muss der Kiosk für die Dauer
# des Tests gestoppt werden (Panel zeigt kurz Konsole/schwarz).
#
# Aufruf auf dem Pi:   sudo bash /opt/smart-display/scripts/pi/test-panel-blank.sh [Sekunden]
# Braucht:             kmsblank aus dem Paket kms++-utils (sudo apt install kms++-utils)
#
# Ergebnis bitte im Ticket #2 eintragen:
#   A) Panel komplett dunkel (kein Glühen)         → DPMS-Off ist ein brauchbarer Nacht-Pfad
#   B) Panel hell mit "No Signal"/blauem Bild       → nur Button-Platine oder Hardware
#   C) Panel dunkel, kommt aber nicht zurück        → Scaler braucht Neu-Sync; nicht nutzbar
set -euo pipefail

SECONDS_OFF="${1:-20}"

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo ausführen." >&2
    exit 1
fi
if ! command -v kmsblank >/dev/null 2>&1; then
    echo "kmsblank fehlt: sudo apt install kms++-utils" >&2
    exit 1
fi

echo "1/3 Kiosk stoppen (Panel wird kurz schwarz/Konsole) …"
systemctl stop smart-display-kiosk
sleep 2

echo "2/3 HDMI-Signal für ${SECONDS_OFF} s abschalten — JETZT aufs Panel schauen."
echo "    Dunkel ohne Glühen = A · hell mit 'No Signal' = B"
# kmsblank hält die Ausgabe dunkel, solange es läuft; timeout beendet es sauber.
timeout --signal=INT "${SECONDS_OFF}" kmsblank || true
sleep 1

echo "3/3 Kiosk wieder starten …"
systemctl start smart-display-kiosk
echo "Fertig. Kommt das Bild innerhalb von ~15 s zurück? Sonst Ergebnis C."
