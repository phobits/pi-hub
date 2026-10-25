#!/usr/bin/env bash
# check-unit-restarts.sh — Gerätetest (Ticket #17): übersteht der Kiosk jede Art
# von Backend-Neustart?
#
# Der Kiosk hängt per Requires= am Backend. Stop und Restart des Backends
# reichen deshalb bis zum Kiosk durch — ein Start nur, weil der Kiosk seit #17
# WantedBy=smart-display.service ist. Ohne das blieb das Panel nach
# `systemctl stop smart-display` + `systemctl start smart-display` dunkel.
# Drei Fälle, je mit Ergebnis:
#   1) systemctl restart smart-display
#   2) systemctl stop smart-display, dann systemctl start smart-display
#   3) Absturz: kill -9 aufs Backend, systemd startet es über Restart=always neu
# "OK" heißt: Das Backend läuft als NEUER Prozess (MainPID gewechselt) und
# antwortet auf /health, der Kiosk läuft und bleibt STABLE_SECONDS lang ohne
# weiteren Neustart, und auf dem Cog-Pfad hat er die Seite nach dem Eingriff
# frisch geladen ("Loaded successfully" hinter dem Journal-Cursor von vorher).
#
# Jeder Fall nimmt dem Panel kurz das Bild. Am Ende laufen beide Units wieder,
# auch wenn ein Fall scheitert oder die SSH-Verbindung abreißt (SIGHUP wird
# ignoriert, Start im EXIT-Trap).
#
# Aufruf auf dem Pi:   sudo bash /opt/smart-display/scripts/pi/check-unit-restarts.sh
# Selbsttest:          vor dem Fix für #17 meldet Fall 2 FAIL, danach alle OK.
# Exit-Code:           0 = alle Fälle OK · 1 = mindestens ein Fall FAIL
set -uo pipefail

BACKEND=smart-display
KIOSK=smart-display-kiosk
INSTALL_DIR=/opt/smart-display
HEALTH_URL="http://127.0.0.1:8080/health"
TIMEOUT_SECONDS=90
STABLE_SECONDS=15

if [[ $EUID -ne 0 ]]; then
    echo "Bitte mit sudo ausführen." >&2
    exit 1
fi

trap '' HUP
trap 'systemctl start "$BACKEND" "$KIOSK"' EXIT

# Gleiche Vorgabe wie start-kiosk.sh: ohne Eintrag Chromium.
KIOSK_BROWSER="$(sed -n 's/^KIOSK_BROWSER=//p' "$INSTALL_DIR/.kiosk.env" 2>/dev/null | tail -1)"
KIOSK_BROWSER="${KIOSK_BROWSER:-chromium}"

kiosk_cursor() {
    journalctl -u "$KIOSK" -n 0 --show-cursor -q | sed -n 's/^-- cursor: //p'
}

wait_healthy() {
    local old_pid="$1" cursor="$2" deadline=$((SECONDS + TIMEOUT_SECONDS))
    until [[ "$(systemctl show -p MainPID --value "$BACKEND")" =~ ^[1-9] \
        && "$(systemctl show -p MainPID --value "$BACKEND")" != "$old_pid" ]] \
        && curl -fs -o /dev/null "$HEALTH_URL"; do
        (( SECONDS < deadline )) || { echo "Backend nicht als neuer Prozess gesund"; return 1; }
        sleep 1
    done
    until [[ "$(systemctl is-active "$KIOSK")" == "active" ]]; do
        (( SECONDS < deadline )) || { echo "Kiosk nicht aktiv: $(systemctl is-active "$KIOSK")"; return 1; }
        sleep 1
    done
    local restarts
    restarts="$(systemctl show -p NRestarts --value "$KIOSK")"
    sleep "$STABLE_SECONDS"
    if [[ "$(systemctl is-active "$KIOSK")" != "active" \
        || "$(systemctl show -p NRestarts --value "$KIOSK")" != "$restarts" ]]; then
        echo "Kiosk nicht stabil (Neustart-Schleife?)"
        return 1
    fi
    if [[ "$KIOSK_BROWSER" == "cog" ]]; then
        local args=(-u "$KIOSK" --no-pager -o cat)
        [[ -n "$cursor" ]] && args+=(--after-cursor "$cursor")
        journalctl "${args[@]}" | grep -q "Loaded successfully" \
            || { echo "Cog hat die Seite nicht frisch geladen"; return 1; }
    fi
    return 0
}

failed=0
run_case() {
    local label="$1"; shift
    local old_pid cursor reason
    old_pid="$(systemctl show -p MainPID --value "$BACKEND")"
    cursor="$(kiosk_cursor)"
    "$@"
    if reason="$(wait_healthy "$old_pid" "$cursor")"; then
        echo "OK    $label"
        return
    fi
    echo "FAIL  $label — $reason"
    failed=1
    # Für den nächsten Fall zurück in einen bekannten Zustand — sonst würde
    # dessen Ergebnis diesem Fall angelastet.
    old_pid="$(systemctl show -p MainPID --value "$BACKEND")"
    cursor="$(kiosk_cursor)"
    systemctl restart "$BACKEND"
    systemctl start "$KIOSK"
    if ! reason="$(wait_healthy "$old_pid" "$cursor")"; then
        echo "ABBRUCH — Grundzustand nicht wiederherstellbar: $reason"
        exit 1
    fi
}

case_restart() { systemctl restart "$BACKEND"; }
case_stop_start() { systemctl stop "$BACKEND"; sleep 2; systemctl start "$BACKEND"; }
case_crash() { systemctl kill -s KILL "$BACKEND"; }

echo "Kiosk-Pfad: $KIOSK_BROWSER"
run_case "1) restart smart-display" case_restart
run_case "2) stop + start smart-display" case_stop_start
run_case "3) Absturz (kill -9, Restart=always)" case_crash
exit "$failed"
