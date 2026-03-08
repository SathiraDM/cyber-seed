#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  setup.sh  —  Interactive one-time configuration
#  Run ONCE before starting the stack for the first time.
#  Safe to re-run to update credentials or target folder.
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[✓]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[✗]${NC}    $*"; exit 1; }
prompt()  { echo -e "${BOLD}$*${NC}"; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║       cyber-seed  —  setup wizard        ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────
command -v docker &>/dev/null || error "Docker not found. Run ./install.sh first."
docker compose version &>/dev/null || error "Docker Compose v2 not found."

# Create dir structure if not already done
mkdir -p config/qbittorrent config/rclone downloads/incomplete downloads/completed logs

# ─────────────────────────────────────────────────────────────────────
#  STEP 1 — OneDrive / SharePoint type
# ─────────────────────────────────────────────────────────────────────
echo ""
prompt "Step 1 — OneDrive type"
echo "  1) Personal OneDrive  (personal Microsoft account)"
echo "  2) SharePoint         (work / organisation / M365)"
echo ""
read -rp "Your choice [1/2]: " OD_TYPE
OD_TYPE="${OD_TYPE:-1}"

case "$OD_TYPE" in
    1) OD_LABEL="Personal OneDrive" ;;
    2) OD_LABEL="SharePoint" ;;
    *) error "Invalid choice." ;;
esac
success "Selected: $OD_LABEL"

# ─────────────────────────────────────────────────────────────────────
#  STEP 2 — rclone remote name
# ─────────────────────────────────────────────────────────────────────
echo ""
prompt "Step 2 — rclone remote name"
echo "  This is the label you'll give this connection (e.g. 'onedrive')."
echo "  It MUST match the ONEDRIVE_REMOTE value in .env"
echo ""
read -rp "Remote name [onedrive]: " REMOTE_NAME
REMOTE_NAME="${REMOTE_NAME:-onedrive}"
success "Remote name: $REMOTE_NAME"

# ─────────────────────────────────────────────────────────────────────
#  STEP 3 — Target folder path
# ─────────────────────────────────────────────────────────────────────
echo ""
prompt "Step 3 — Target folder on $OD_LABEL"
if [[ "$OD_TYPE" == "2" ]]; then
    echo "  SharePoint example: /sites/MySiteName/Shared Documents/Torrents"
    echo "  (Use the path as it appears in the SharePoint URL / document library)"
    read -rp "Target path: " REMOTE_PATH
    REMOTE_PATH="${REMOTE_PATH:-/Shared Documents/Torrents}"
else
    echo "  Example: /Torrents   or   /Downloads/Seeded"
    read -rp "Target path [/Torrents]: " REMOTE_PATH
    REMOTE_PATH="${REMOTE_PATH:-/Torrents}"
fi
success "Target path: $REMOTE_PATH"

# ─────────────────────────────────────────────────────────────────────
#  STEP 4 — rclone authentication
# ─────────────────────────────────────────────────────────────────────
echo ""
prompt "Step 4 — OneDrive authentication via rclone"
echo ""
echo "  Does this server have a web browser available?"
echo "  1) Yes — I can open a browser on THIS machine (local/dev)"
echo "  2) No  — Headless server (SSH only) — I'll auth on another machine"
echo ""
read -rp "Your choice [1/2]: " HEADLESS_CHOICE
HEADLESS_CHOICE="${HEADLESS_CHOICE:-2}"

echo ""

# Build rclone config inside a temporary container (no host rclone needed)
RCLONE_CONF_HOST="$(pwd)/config/rclone/rclone.conf"

if [[ "$HEADLESS_CHOICE" == "2" ]]; then
    # ── Headless: generate token on another machine ───────────────────
    echo -e "${YELLOW}Headless authentication flow:${NC}"
    echo ""
    echo "  On a machine WITH a browser (laptop, desktop), run:"
    echo -e "  ${CYAN}docker run --rm rclone/rclone authorize \"onedrive\"${NC}"
    echo ""
    echo "  Follow the browser prompt, then paste the token JSON below."
    echo "  (It looks like: {\"access_token\":\"...\",\"token_type\":\"Bearer\",...})"
    echo ""
    read -rp "Paste token JSON here: " RCLONE_TOKEN

    if [[ -z "$RCLONE_TOKEN" ]]; then
        error "No token provided. Re-run setup.sh when ready."
    fi

    # Write rclone.conf manually
    mkdir -p config/rclone

    if [[ "$OD_TYPE" == "2" ]]; then
        # SharePoint requires drive_type = sharepoint
        cat > "$RCLONE_CONF_HOST" <<EOF
[$REMOTE_NAME]
type = onedrive
token = $RCLONE_TOKEN
drive_type = sharepoint
EOF
        echo ""
        warn "For SharePoint you also need the drive_id."
        echo "  Run this to list your SharePoint sites:"
        echo -e "  ${CYAN}docker run --rm -v $(pwd)/config/rclone:/config/rclone rclone/rclone --config /config/rclone/rclone.conf onedrive sites${NC}"
        echo ""
        read -rp "SharePoint drive_id (leave blank to skip and set manually): " SP_DRIVE_ID
        if [[ -n "$SP_DRIVE_ID" ]]; then
            echo "drive_id = $SP_DRIVE_ID" >> "$RCLONE_CONF_HOST"
        fi
    else
        cat > "$RCLONE_CONF_HOST" <<EOF
[$REMOTE_NAME]
type = onedrive
token = $RCLONE_TOKEN
drive_type = personal
EOF
    fi
    chmod 600 "$RCLONE_CONF_HOST"
    success "rclone.conf written."

else
    # ── Browser auth on this machine ──────────────────────────────────
    info "Starting rclone config in Docker (browser will open) ..."
    mkdir -p config/rclone

    docker run --rm -it \
        -v "$(pwd)/config/rclone:/config/rclone" \
        -e RCLONE_CONFIG=/config/rclone/rclone.conf \
        rclone/rclone config create "$REMOTE_NAME" onedrive \
        || warn "rclone config create finished (this may be intentional)."

    [[ -f "$RCLONE_CONF_HOST" ]] || error "rclone.conf was not created. Re-run setup.sh."
    chmod 600 "$RCLONE_CONF_HOST"
    success "rclone.conf written."
fi

# ─────────────────────────────────────────────────────────────────────
#  STEP 5 — Verify rclone connection
# ─────────────────────────────────────────────────────────────────────
echo ""
info "Verifying rclone connection to ${REMOTE_NAME}:${REMOTE_PATH} ..."

VERIFY=$(docker run --rm \
    -v "$(pwd)/config/rclone:/config/rclone" \
    -e RCLONE_CONFIG=/config/rclone/rclone.conf \
    rclone/rclone lsd "${REMOTE_NAME}:/" 2>&1 || true)

if echo "$VERIFY" | grep -qi "error\|failed\|invalid"; then
    warn "rclone verification had issues:"
    echo "$VERIFY"
    warn "You can continue but check your credentials."
else
    success "rclone connection verified."
fi

# ─────────────────────────────────────────────────────────────────────
#  STEP 6 — Host user IDs
# ─────────────────────────────────────────────────────────────────────
PUID=$(id -u)
PGID=$(id -g)
info "Detected PUID=$PUID PGID=$PGID"

# ─────────────────────────────────────────────────────────────────────
#  STEP 7 — Ports
# ─────────────────────────────────────────────────────────────────────
echo ""
prompt "Step 5 — Port configuration"
read -rp "qBittorrent Web UI port [8080]: " WEBUI_PORT
WEBUI_PORT="${WEBUI_PORT:-8080}"
read -rp "Torrent traffic port [6881]: " TORRENT_PORT
TORRENT_PORT="${TORRENT_PORT:-6881}"
read -rp "Glances monitor port [61208]: " MONITOR_PORT
MONITOR_PORT="${MONITOR_PORT:-61208}"

# ─────────────────────────────────────────────────────────────────────
#  STEP 6 — Credentials  (username is always: admin)
# ─────────────────────────────────────────────────────────────────────
echo ""
prompt "Step 6 — Set passwords  (username for both services will be: admin)"
echo ""

# ── qBittorrent password ──────────────────────────────────────────────
prompt "  qBittorrent Web UI password:"
while true; do
    read -rsp "    Enter password: " QBT_WEBUI_PASS; echo ""
    [[ -z "$QBT_WEBUI_PASS" ]] && warn "    Password cannot be empty. Try again." && continue
    read -rsp "    Confirm password: " QBT_WEBUI_PASS2; echo ""
    [[ "$QBT_WEBUI_PASS" == "$QBT_WEBUI_PASS2" ]] && break
    warn "    Passwords do not match. Try again."
done
success "qBittorrent password set."

echo ""

# ── Glances password ──────────────────────────────────────────────────
prompt "  Glances monitor password:"
while true; do
    read -rsp "    Enter password: " MONITOR_PASS; echo ""
    [[ -z "$MONITOR_PASS" ]] && warn "    Password cannot be empty. Try again." && continue
    read -rsp "    Confirm password: " MONITOR_PASS2; echo ""
    [[ "$MONITOR_PASS" == "$MONITOR_PASS2" ]] && break
    warn "    Passwords do not match. Try again."
done
success "Glances password set."

# Username is always admin — no need to prompt
MONITOR_USER="admin"

# ─────────────────────────────────────────────────────────────────────
#  STEP 8 — Write .env
# ─────────────────────────────────────────────────────────────────────
cat > .env <<EOF
# Generated by setup.sh — $(date)
PUID=$PUID
PGID=$PGID
TZ=$(cat /etc/timezone 2>/dev/null || echo UTC)
WEBUI_PORT=$WEBUI_PORT
TORRENT_PORT=$TORRENT_PORT
MONITOR_PORT=$MONITOR_PORT
MONITOR_USER=admin
MONITOR_PASS=$MONITOR_PASS
QBT_WEBUI_PASS=$QBT_WEBUI_PASS
DOWNLOADS_PATH=./downloads
ONEDRIVE_REMOTE=$REMOTE_NAME
ONEDRIVE_PATH=$REMOTE_PATH
EOF

chmod 600 .env
success ".env written."

# ─────────────────────────────────────────────────────────────────────
#  Done
# ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo -e "${GREEN}  Setup complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo ""
echo "  What was configured:"
echo -e "    Remote  : ${CYAN}${REMOTE_NAME}${NC}"
echo -e "    Target  : ${CYAN}${REMOTE_PATH}${NC}"
echo -e "    Web UI  : ${CYAN}http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):${WEBUI_PORT}${NC}"
echo -e "    Monitor : ${CYAN}http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):${MONITOR_PORT}${NC}"
echo ""
echo "  Next:"
echo -e "    ${YELLOW}./start.sh${NC}   ← Launch the stack"
echo ""
echo "  qBittorrent  →  admin / [password you set]"
echo "  Glances      →  admin / [password you set]"
echo ""
