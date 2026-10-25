#!/usr/bin/env bash
# sync-album.sh — Zieht das Lightroom-Album sofort komplett auf den Pi (#15).
#
# Der Scheduler lädt pro 30-Minuten-Tick nur ein neues Foto
# (SCREENSAVER_IMAGES_PER_TICK=1, bewusst). Dieses Skript gleicht alles auf
# einmal ab: neue Fotos, in Lightroom bearbeitete und solche, die aus dem Album
# entfernt wurden. Die Logik ist dieselbe wie im Provider
# (smart_display/album_sync.py ruft sync_remote_images auf), nur ohne
# Download-Limit und ohne Backoff.
#
# Warum beide Units gestoppt werden:
#   - Das Backend hält das Manifest im Speicher und schreibt es beim nächsten
#     Tick zurück — dabei löscht es jede Datei, die es nicht kennt, also auch
#     alles, was dieser Lauf geladen hat. album_sync verweigert deshalb, solange
#     /health antwortet.
#   - Der Kiosk hängt per Requires= am Backend und wird ohnehin mitgestoppt.
#     Seit #17 startet `systemctl start smart-display` ihn auch wieder mit; beide
#     Units stehen hier trotzdem ausdrücklich da, damit das Skript auch auf
#     einem Pi mit älteren Units (vor install-pi.sh mit #17) das Panel nicht
#     dunkel zurücklässt.
# Das Starten steht in einem EXIT-Trap und SIGHUP wird ignoriert: Auch ein
# fehlgeschlagener Abgleich oder eine abgerissene SSH-Verbindung lassen das
# Panel nicht aus. `sudo -n`, weil das Remote-Skript über stdin kommt: eine
# Passwortabfrage würde den Rest des Skripts als Passwort lesen.
#
# Aufruf:
#   bash scripts/sync-album.sh            # abgleichen (Panel ist ~1 Minute aus)
#   bash scripts/sync-album.sh --dry-run  # nur anzeigen, Dienste laufen weiter
#
# Konfiguration:
#   PI_HOST=pi@192.168.178.22   Zielgerät (user@host)
#   PI_DIR=/opt/smart-display   Installationsverzeichnis auf dem Pi
#
# Exit-Codes: 0 = erledigt · 3 = Pi nicht erreichbar, nichts verändert
#             jeder andere ≠ 0 = Abgleich fehlgeschlagen, Dienste laufen wieder
#
set -euo pipefail

PI_HOST="${PI_HOST:-pi@192.168.178.22}"
PI_DIR="${PI_DIR:-/opt/smart-display}"
HEALTH_URL="http://127.0.0.1:8080/health"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        *)
            echo "Unbekanntes Argument: $1 (erlaubt: --dry-run)" >&2
            exit 1
            ;;
    esac
    shift
done

if ! ssh_err="$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI_HOST" true 2>&1)"; then
    echo "WARNUNG: ${PI_HOST} nicht erreichbar — nichts verändert." >&2
    [[ -n "$ssh_err" ]] && echo "         Ursache: ${ssh_err}" >&2
    exit 3
fi

if [[ "$DRY_RUN" == "true" ]]; then
    ssh -o BatchMode=yes "$PI_HOST" \
        "cd '$PI_DIR' && .venv/bin/python -m smart_display.album_sync --dry-run"
    exit 0
fi

ssh -o BatchMode=yes "$PI_HOST" bash -s <<EOF
set -u
cd '$PI_DIR'
trap '' HUP
restart_units() {
    sudo -n systemctl start smart-display smart-display-kiosk
    for _ in \$(seq 1 30); do
        curl -fs -o /dev/null '$HEALTH_URL' && break
        sleep 1
    done
    echo "smart-display: \$(systemctl is-active smart-display) · kiosk: \$(systemctl is-active smart-display-kiosk)"
}
trap restart_units EXIT
echo "=== Kiosk und Backend stoppen ==="
sudo -n systemctl stop smart-display-kiosk smart-display
echo "=== Album abgleichen ==="
.venv/bin/python -m smart_display.album_sync
EOF
