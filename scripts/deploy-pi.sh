#!/usr/bin/env bash
# deploy-pi.sh — Pushes the current working tree onto the running Pi.
#
# This is the *update* path, not the install path. `install-pi.sh` provisions a
# fresh machine (apt packages, venv, systemd units) and is the wrong tool for
# shipping a code change: it reinstalls the world for what is usually a handful
# of static files. This script only syncs the code and restarts the two units.
#
# Safe by construction: the rsync excludes everything that lives *only* on the
# device — credentials (.env, .kiosk.env), the state cache and screensaver
# images (data/), the venv, and the editable-install egg-info. Nothing here
# recreates those, so deleting them would take the device down until the full
# installer is run again.
#
# Aufruf:
#   bash scripts/deploy-pi.sh                  # sync + Restart nur wenn nötig
#   bash scripts/deploy-pi.sh --dry-run        # zeigt nur, was sich ändern würde
#   bash scripts/deploy-pi.sh --force-restart  # Restart erzwingen (Gerät driftet)
#
# Der Restart ist bedingt, weil /ship dieses Skript bei JEDEM Ship aufruft. Doku,
# Tests und CI erreichen das Gerät gar nicht erst (RSYNC_SKIP unten) — rsync
# überträgt dann nichts, und das Panel bleibt an. Sobald rsync irgendeine Datei
# überträgt oder löscht, ist sie per Definition geräterelevant: Neustart. Das
# ganze Kriterium ist "rsync-Ausgabe leer oder nicht" — kein Parsen, kein
# Ermessen. Bewusst so, weil der Mac openrsync fährt und der Pi GNU rsync: die
# beiden formatieren --itemize-changes unterschiedlich, aber "leer" ist gleich.
#
# Konfiguration:
#   PI_HOST=pi@192.168.178.22   Zielgerät (user@host)
#   PI_DIR=/opt/smart-display   Installationsverzeichnis auf dem Pi
#
# Exit-Codes: 0 = erledigt · 3 = Pi nicht erreichbar, es wurde nichts deployed
#
set -euo pipefail

cd "$(dirname "$0")/.."

PI_HOST="${PI_HOST:-pi@192.168.178.22}"
PI_DIR="${PI_DIR:-/opt/smart-display}"
HEALTH_URL="http://127.0.0.1:8080/health"
DRY_RUN=false
FORCE_RESTART=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)       DRY_RUN=true ;;
        --force-restart) FORCE_RESTART=true ;;
        *)
            echo "Unbekanntes Argument: $1 (erlaubt: --dry-run, --force-restart)" >&2
            exit 1
            ;;
    esac
    shift
done

# Anything the device owns and the repo cannot recreate, plus local dirt that
# rsync would otherwise ship (it does not read .gitignore). install-pi.sh has a
# deliberately shorter list — it lays down a full checkout — so the two do not
# match line for line; only the device-owned entries here are load-bearing.
RSYNC_OWNED=(
    --exclude '.venv'
    --exclude 'data'
    # Keep deployable templates, but never let --delete remove device-side
    # credential backups such as .env.before-<date>.
    --include '.env.example'
    --include '.env.local.example'
    --exclude '.env'
    --exclude '.env.*'
    --exclude '.kiosk.env*'
    --exclude '__pycache__'
    --exclude '*.py[cod]'
    --exclude '.git'
    --exclude '*.egg-info'
    --exclude '.claude'
    # rsync kennt .gitignore nicht. Ohne diese Zeilen schreibt der Finder bei
    # jedem Fensterwechsel ein .DS_Store neu — das Restart-Gate unten würde
    # daraufhin bei JEDEM Ship neu starten, auch beim reinen Doku-Ship.
    --exclude '.DS_Store'
    --exclude '.pytest_cache'
    --exclude '.mypy_cache'
    --exclude '.ruff_cache'
    --exclude '/build'
    --exclude '/dist'
)

# Deploy-only: nichts davon wird zur Laufzeit gelesen. Sie auszuschließen ist
# das, was das Restart-Gate unten trivial macht — ein reiner Doku-Ship überträgt
# dann nachweislich null Dateien. NICHT nach install-pi.sh spiegeln, der
# Installer legt den Vollcheckout an. Falls hier je etwas dazukommt, das doch
# aufs Gerät muss, gehört es aus dieser Liste raus — keine Ausnahme einbauen.
# Verankert (führender /), damit ein künftiges smart_display/providers/tests/
# nicht lautlos vom Deploy ausgenommen wird. `*.md` bleibt bewusst unverankert.
RSYNC_SKIP=(
    --exclude '/docs'
    --exclude '/tests'
    --exclude '/.github'
    --exclude '*.md'
)

RSYNC_EXCLUDES=("${RSYNC_OWNED[@]}" "${RSYNC_SKIP[@]}")

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Hinweis: Working Tree ist nicht sauber — es wird der Arbeitsstand"
    echo "         deployed, nicht der letzte Commit."
fi

# Erreichbarkeit zuerst: dieses Skript läuft ab jetzt bei jedem Ship, auch wenn
# der Pi aus ist. Ein schlafendes Gerät ist kein kaputter Ship — aber auch kein
# grüner. Eigener Exit-Code, damit /ship das unterscheiden kann.
if ! ssh_err="$(ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI_HOST" true 2>&1)"; then
    echo "WARNUNG: ${PI_HOST} nicht erreichbar — es wurde NICHTS deployed." >&2
    # Die Ursache mit ausgeben: ein geänderter Hostkey oder ein abgelaufener
    # Agent sähe sonst aus wie ein schlafendes Gerät und würde als harmlos
    # verbucht.
    [[ -n "$ssh_err" ]] && echo "         Ursache: ${ssh_err}" >&2
    echo "         Der Push ist raus, das Gerät läuft weiter auf altem Stand." >&2
    echo "         Nachholen: bash scripts/deploy-pi.sh" >&2
    exit 3
fi

echo "=== 1/3  Code nach ${PI_HOST}:${PI_DIR} synchronisieren ==="
CHANGED="$(mktemp)"
trap 'rm -f "$CHANGED"' EXIT
rsync_args=(-a --delete --itemize-changes
    -e 'ssh -o ConnectTimeout=10 -o BatchMode=yes'
    "${RSYNC_EXCLUDES[@]}")
if [[ "$DRY_RUN" == "true" ]]; then
    rsync_args+=(--dry-run)
fi
rsync "${rsync_args[@]}" ./ "${PI_HOST}:${PI_DIR}/" | tee "$CHANGED"

# Leere Ausgabe = identische Bäume = nichts zu tun. Bei einem echten No-op gibt
# rsync keine einzige Zeile aus, auch keine Verzeichniszeilen.
#
# Bewusst grob: rsyncs Quick-Check ist Größe+mtime, keine Prüfsumme. Eine Datei,
# die inhaltlich gleich ist, aber eine neuere mtime hat (git checkout, stash pop,
# Branch-Wechsel), wird als `<f..t....` gemeldet und löst hier einen Neustart
# aus. Das ist die richtige Fehlerrichtung — lieber einmal zu viel neu starten
# als Code ausliefern und den Restart verschlucken. Für den Fall, der das Gate
# begründet (reiner Doku-Ship), tritt es nicht auf: dabei fasst niemand die
# mtime einer Code-Datei an. Nach einem Deploy sind die mtimes wieder synchron.
# Aber: rsync ist idempotent, das Gate kennt nur DIESEN Lauf. Bricht ein Lauf
# nach dem Sync ab (Health-Timeout, Ctrl-C, hängendes ssh), liefert der Retry
# null Zeilen — der fällige Restart würde verschluckt und der Report wäre grün,
# während alte Prozesse neuen Code bedienen. Vorher konnte das nicht passieren,
# weil unbedingt neu gestartet wurde. Deshalb die Restart-Pflicht persistieren:
# Marker in data/ (rsync-excluded, überlebt jeden Sync), gelöscht erst nachdem
# der Kiosk wirklich neu gestartet ist.
PENDING="$PI_DIR/data/.restart-pending"
if [[ -s "$CHANGED" ]]; then
    RELEVANT=true
    if [[ "$DRY_RUN" != "true" ]]; then
        ssh -o BatchMode=yes "$PI_HOST" "mkdir -p '$PI_DIR/data' && touch '$PENDING'"
    fi
elif [[ "$DRY_RUN" != "true" ]] && ssh -o BatchMode=yes "$PI_HOST" "test -f '$PENDING'"; then
    echo "Offener Restart aus einem abgebrochenen Lauf — wird nachgeholt."
    RELEVANT=true
else
    RELEVANT=false
fi

if [[ "$DRY_RUN" == "true" ]]; then
    echo
    if [[ "$FORCE_RESTART" == "true" || "$RELEVANT" == "true" ]]; then
        echo "Dry-Run — nichts verändert. Neustart wäre nötig: ja"
    else
        echo "Dry-Run — nichts verändert. Neustart wäre nötig: nein"
    fi
    exit 0
fi

if [[ "$FORCE_RESTART" != "true" && "$RELEVANT" != "true" ]]; then
    echo "=== 2/3  Neustart übersprungen — nichts Geräterelevantes geändert ==="
    echo "         Das Panel bleibt an."
else
    # Backend first, then the browser: restarting the kiosk is what reloads the
    # page, so doing it last means the panel comes back on the new code rather
    # than on a page it fetched mid-restart.
    #
    # daemon-reload ist reine Hygiene und KANN eine Unit-Änderung nicht aktivieren:
    # install-pi.sh kopiert die Units nach /etc/systemd/system (kein Symlink), der
    # rsync erreicht nur /opt/smart-display/deploy/systemd/. Ein geändertes
    # *.service braucht install-pi.sh — sonst ist der Report grün und die
    # Änderung wirkungslos.
    echo "=== 2/3  Dienste neu starten ==="
    ssh -o BatchMode=yes "$PI_HOST" "sudo systemctl daemon-reload"
    ssh -o BatchMode=yes "$PI_HOST" "sudo systemctl restart smart-display"
    ssh -o BatchMode=yes "$PI_HOST" "
        for _ in \$(seq 1 30); do
            curl -fs -o /dev/null '$HEALTH_URL' && exit 0
            sleep 1
        done
        echo 'Backend meldet sich nicht auf $HEALTH_URL' >&2
        exit 1
    "
    ssh -o BatchMode=yes "$PI_HOST" "sudo systemctl restart smart-display-kiosk"
    # Erst jetzt ist die Restart-Pflicht wirklich erfüllt.
    ssh -o BatchMode=yes "$PI_HOST" "rm -f '$PENDING'"
fi

echo "=== 3/3  Prüfen ==="
ssh -o BatchMode=yes "$PI_HOST" "
    $([[ "$RELEVANT" == "true" || "$FORCE_RESTART" == "true" ]] && echo 'sleep 5')
    printf '  Version:  %s\n' \"\$(sed -n 's/^__version__ = \"\(.*\)\"/\1/p' '$PI_DIR/smart_display/__init__.py')\"
    printf '  Backend:  %s\n' \"\$(systemctl is-active smart-display)\"
    printf '  Kiosk:    %s (Restarts: %s)\n' \\
        \"\$(systemctl is-active smart-display-kiosk)\" \\
        \"\$(systemctl show smart-display-kiosk -p NRestarts --value)\"
    curl -fs -o /dev/null -w '  Health:   %{http_code}\n' '$HEALTH_URL'
"

echo
if [[ "$RELEVANT" == "true" || "$FORCE_RESTART" == "true" ]]; then
    echo "Deployed. Das Panel lässt sich nicht per Screenshot prüfen — Cog rendert"
    echo "direkt auf DRM. Kontrolle nur mit einem Blick aufs Gerät."
else
    echo "Nichts zu deployen — Gerät war bereits auf Stand, das Panel lief durch."
fi
