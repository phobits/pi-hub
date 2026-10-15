#!/usr/bin/env bash
# install-pi.sh — Deploys smart-display onto a fresh Raspberry Pi OS Lite.
#
# Voraussetzungen:
#   - SD-Karte geflasht mit Raspberry Pi OS Lite (64-bit empfohlen)
#   - SSH, WLAN und ein Admin-User im Imager konfiguriert
#   - Dieses Repo ist bereits auf den Pi kopiert (git clone oder scp)
#
# Aufruf:
#   sudo bash scripts/install-pi.sh
# Optional, falls ein anderer Service-User verwendet werden soll:
#   sudo env SMART_DISPLAY_USER=pi bash scripts/install-pi.sh
#
set -euo pipefail

INSTALL_DIR="/opt/smart-display"
SERVICE_USER="${SMART_DISPLAY_USER:-${SUDO_USER:-pi}}"
if [[ "$SERVICE_USER" == "root" && -z "${SMART_DISPLAY_USER:-}" ]]; then
    SERVICE_USER="pi"
fi
REQUESTED_KIOSK_BROWSER="${SMART_DISPLAY_KIOSK_BROWSER:-}"
INSTALL_COG="${SMART_DISPLAY_INSTALL_COG:-false}"
case "$REQUESTED_KIOSK_BROWSER" in
    "" | chromium | chromium-browser | cog)
        ;;
    *)
        echo "Fehler: SMART_DISPLAY_KIOSK_BROWSER muss chromium, chromium-browser oder cog sein." >&2
        exit 1
        ;;
esac
if [[ "$REQUESTED_KIOSK_BROWSER" == "cog" ]]; then
    INSTALL_COG="true"
fi

# --- Preflight ---------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    echo "Fehler: als root ausführen → sudo bash $0" >&2
    exit 1
fi

if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Fehler: User '$SERVICE_USER' existiert nicht."
    echo "Erstelle ihn im Raspberry Pi Imager oder setze SMART_DISPLAY_USER auf den richtigen User." >&2
    exit 1
fi
SERVICE_GROUP="${SMART_DISPLAY_GROUP:-$(id -gn "$SERVICE_USER")}"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [[ ! -f "$SCRIPT_DIR/pyproject.toml" ]]; then
    echo "Fehler: Script muss aus dem Repository-Root ausgeführt werden." >&2
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Smart Display — Pi Installation        ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# --- 1. System-Pakete --------------------------------------------------------

echo "=== 1/8  System-Pakete installieren ==="
apt-get update -qq
is_installable_package() {
    local candidate
    candidate="$(apt-cache policy "$1" | awk '/Candidate:/ {print $2; exit}')"
    [[ -n "$candidate" && "$candidate" != "(none)" ]]
}

CHROMIUM_PACKAGE="chromium-browser"
if ! is_installable_package "$CHROMIUM_PACKAGE"; then
    CHROMIUM_PACKAGE="chromium"
fi
if ! is_installable_package "$CHROMIUM_PACKAGE"; then
    echo "Fehler: weder chromium-browser noch chromium ist installierbar." >&2
    exit 1
fi

OPTIONAL_KIOSK_PACKAGES=()
# Ticket #1: compressed swap in RAM — no SD writes, buys headroom for WPE on
# 512 MB. SMART_DISPLAY_ZRAM=false skips it.
if [[ "${SMART_DISPLAY_ZRAM:-true}" == "true" ]]; then
    OPTIONAL_KIOSK_PACKAGES+=(zram-tools)
fi
if [[ "$INSTALL_COG" == "true" ]]; then
    for package in cog wpewebkit-driver libgles2; do
        if ! is_installable_package "$package"; then
            echo "Fehler: KIOSK_BROWSER=cog angefordert, aber Paket '$package' ist nicht installierbar." >&2
            exit 1
        fi
    done
    OPTIONAL_KIOSK_PACKAGES+=(cog wpewebkit-driver libgles2)
fi

apt-get install -y --no-install-recommends \
    python3-venv \
    python3-dev \
    git \
    rsync \
    xserver-xorg \
    xserver-xorg-legacy \
    x11-xserver-utils \
    xinit \
    openbox \
    "$CHROMIUM_PACKAGE" \
    "${OPTIONAL_KIOSK_PACKAGES[@]}" \
    fonts-noto-core \
    fonts-inter \
    libopenjp2-7 \
    libtiff6 \
    libjpeg62-turbo
echo "  Chromium-Paket: $CHROMIUM_PACKAGE"
if [[ ${#OPTIONAL_KIOSK_PACKAGES[@]} -gt 0 ]]; then
    echo "  Optionale Kiosk-Pakete: ${OPTIONAL_KIOSK_PACKAGES[*]}"
fi
echo "  OK"

# --- 2. Projekt deployen -----------------------------------------------------

echo "=== 2/8  Projekt nach $INSTALL_DIR deployen ==="
if [[ "$(realpath "$SCRIPT_DIR")" != "$(realpath "$INSTALL_DIR")" ]]; then
    mkdir -p "$INSTALL_DIR"
    rsync -a --delete \
        --exclude '.venv' \
        --exclude 'data' \
        --exclude '.env' \
        --exclude '.env.local' \
        --exclude '.kiosk.env' \
        --exclude '__pycache__' \
        "$SCRIPT_DIR/" "$INSTALL_DIR/"
    echo "  Kopiert von $SCRIPT_DIR"
else
    echo "  Repo ist bereits in $INSTALL_DIR"
fi
chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR"

# --- 3. Python venv + Dependencies -------------------------------------------

echo "=== 3/8  Python-Umgebung einrichten ==="
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip --quiet
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -e "$INSTALL_DIR" --quiet
echo "  OK"

# --- 4. .env ------------------------------------------------------------------

echo "=== 4/8  Konfiguration ==="
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    echo "  .env angelegt (aus .env.example) — Credentials müssen noch eingetragen werden!"
else
    echo "  .env existiert bereits — wird nicht überschrieben."
fi

if [[ ! -f "$INSTALL_DIR/.kiosk.env" ]]; then
    cp "$INSTALL_DIR/deploy/x11/kiosk.env.example" "$INSTALL_DIR/.kiosk.env"
    if [[ -n "$REQUESTED_KIOSK_BROWSER" ]]; then
        sed -i "s/^KIOSK_BROWSER=.*/KIOSK_BROWSER=$REQUESTED_KIOSK_BROWSER/" "$INSTALL_DIR/.kiosk.env"
    fi
    chown "$SERVICE_USER":"$SERVICE_GROUP" "$INSTALL_DIR/.kiosk.env"
    chmod 600 "$INSTALL_DIR/.kiosk.env"
    echo "  .kiosk.env angelegt (aus kiosk.env.example)"
else
    echo "  .kiosk.env existiert bereits — wird nicht überschrieben."
fi

# --- 5. Datenverzeichnis -----------------------------------------------------

echo "=== 5/8  Datenverzeichnis ==="
DATA_DIR="$INSTALL_DIR/data"
mkdir -p "$DATA_DIR/screensaver" "$DATA_DIR/measurements"
chown -R "$SERVICE_USER":"$SERVICE_GROUP" "$DATA_DIR"
echo "  $DATA_DIR bereit"

# --- 6. X11 / Kiosk ----------------------------------------------------------

echo "=== 6/8  Kiosk vorbereiten ==="
chmod +x "$INSTALL_DIR/deploy/x11/kiosk-session.sh"
chmod +x "$INSTALL_DIR/deploy/kiosk/start-kiosk.sh"
chmod +x "$INSTALL_DIR/scripts/measure_pi.py" "$INSTALL_DIR/scripts/pi/test-panel-blank.sh"

# xinit braucht diese Einstellung damit ein non-root User X starten darf
XWRAPPER="/etc/X11/Xwrapper.config"
if [[ ! -f "$XWRAPPER" ]] \
    || ! grep -q "allowed_users=anybody" "$XWRAPPER" \
    || ! grep -q "needs_root_rights=yes" "$XWRAPPER"; then
    mkdir -p /etc/X11
    {
        echo "allowed_users=anybody"
        echo "needs_root_rights=yes"
    } > "$XWRAPPER"
    echo "  Xwrapper.config: allowed_users=anybody, needs_root_rights=yes gesetzt"
fi

# Console-Autologin deaktivieren falls aktiv — systemd managed den Kiosk
echo "  OK"

# --- 7. Systemd ---------------------------------------------------------------

echo "=== 7/8  Systemd-Services ==="
sed \
    -e "s/^User=.*/User=$SERVICE_USER/" \
    -e "s/^Group=.*/Group=$SERVICE_GROUP/" \
    "$INSTALL_DIR/deploy/systemd/smart-display.service" \
    > /etc/systemd/system/smart-display.service
sed \
    -e "s/^User=.*/User=$SERVICE_USER/" \
    -e "s/^Group=.*/Group=$SERVICE_GROUP/" \
    "$INSTALL_DIR/deploy/systemd/smart-display-kiosk.service" \
    > /etc/systemd/system/smart-display-kiosk.service
systemctl daemon-reload
systemctl enable smart-display.service smart-display-kiosk.service
echo "  Services aktiviert für $SERVICE_USER:$SERVICE_GROUP (starten beim nächsten Boot)"

# --- 8. Dauerbetrieb (Ticket #1) + Ausschalten per Touch (Ticket #2) --------

echo "=== 8/8  Dauerbetrieb: journald, Watchdog, zram, sudo ==="

# journald-Deckel — sonst frisst eine gesprächige Woche die SD-Karte.
install -d -m 755 /etc/systemd/journald.conf.d
install -m 644 "$INSTALL_DIR/deploy/systemd/journald-smart-display.conf" \
    /etc/systemd/journald.conf.d/smart-display.conf
systemctl restart systemd-journald
echo "  journald: SystemMaxUse=32M"

# Hardware-Watchdog: systemd füttert /dev/watchdog, ein hängender Kernel
# rebootet nach 15 s. Braucht dtparam=watchdog=on in config.txt.
CONFIG_TXT="/boot/firmware/config.txt"
[[ -f "$CONFIG_TXT" ]] || CONFIG_TXT="/boot/config.txt"
if [[ -f "$CONFIG_TXT" ]]; then
    if ! grep -qE '^dtparam=watchdog=on' "$CONFIG_TXT"; then
        printf '\n# smart-display: Hardware-Watchdog (Ticket #1)\ndtparam=watchdog=on\n' >> "$CONFIG_TXT"
        echo "  config.txt: dtparam=watchdog=on ergänzt (wirkt nach Reboot)"
    fi
    # Optional: GPIO3 (Pin 5) gegen GND weckt den gehaltenen Pi wieder auf und
    # löst im Betrieb einen sauberen Shutdown aus. Nur auf Wunsch, weil nicht
    # geprüft ist, ob das Display-Board den Pin belegt.
    if [[ "${SMART_DISPLAY_GPIO_SHUTDOWN:-false}" == "true" ]] \
        && ! grep -qE '^dtoverlay=gpio-shutdown' "$CONFIG_TXT"; then
        printf '\n# smart-display: Pin 5 auf GND = Shutdown / Aufwecken (Ticket #2)\ndtoverlay=gpio-shutdown\n' >> "$CONFIG_TXT"
        echo "  config.txt: dtoverlay=gpio-shutdown ergänzt (wirkt nach Reboot)"
    fi
else
    echo "  Warnung: config.txt nicht gefunden — Watchdog-dtparam manuell setzen." >&2
fi
install -d -m 755 /etc/systemd/system.conf.d
install -m 644 "$INSTALL_DIR/deploy/systemd/system-watchdog.conf" \
    /etc/systemd/system.conf.d/smart-display-watchdog.conf
echo "  systemd: RuntimeWatchdogSec=15 (wirkt nach Reboot bzw. daemon-reexec)"

# zram-Swap (128 MB komprimiert im RAM, keine SD-Schreibzugriffe).
if [[ "${SMART_DISPLAY_ZRAM:-true}" == "true" && -f /etc/default/zramswap ]]; then
    sed -i -e 's/^#\?SIZE=.*/SIZE=128/' -e 's/^#\?PERCENT=.*/#PERCENT=/' /etc/default/zramswap
    grep -qE '^SIZE=' /etc/default/zramswap || echo "SIZE=128" >> /etc/default/zramswap
    systemctl enable zramswap.service >/dev/null 2>&1 || true
    echo "  zram: 128 MB (aktiv nach Reboot)"
fi

# sudo-Regel: genau zwei Kommandos für den Ausschaltknopf, nichts sonst.
SUDOERS_TMP="$(mktemp)"
sed "s/^pi /$SERVICE_USER /" "$INSTALL_DIR/deploy/sudoers/smart-display" > "$SUDOERS_TMP"
if visudo -cf "$SUDOERS_TMP" >/dev/null; then
    install -m 440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/smart-display
    echo "  sudoers: $SERVICE_USER darf systemctl poweroff|reboot"
else
    echo "  Fehler: sudoers-Regel ungültig, nicht installiert." >&2
fi
rm -f "$SUDOERS_TMP"
echo "  Hinweis: APP_ALLOW_SHUTDOWN=true in $INSTALL_DIR/.env schaltet den Knopf frei."

# --- Fertig -------------------------------------------------------------------

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Installation abgeschlossen!            ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Nächste Schritte:"
echo ""
echo "  1. Credentials eintragen:"
echo "     nano $INSTALL_DIR/.env"
echo ""
echo "  2. Backend starten + Logs prüfen:"
echo "     sudo systemctl start smart-display"
echo "     journalctl -u smart-display -f"
echo ""
echo "  3. Kiosk starten (Display muss angeschlossen sein):"
echo "     sudo systemctl start smart-display-kiosk"
echo "     journalctl -u smart-display-kiosk -f"
echo ""
echo "  4. Oder einfach rebooten — beide Services starten automatisch:"
echo "     sudo reboot"
echo ""
