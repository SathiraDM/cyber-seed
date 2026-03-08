#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  firewall.sh  —  Open required ports for cyber-seed
#  Supports: GCP (gcloud), ufw (Ubuntu), iptables (fallback)
#  Run once after install.sh, before start.sh
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[✓]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[✗]${NC}    $*"; exit 1; }

# Load ports from .env if present
WEBUI_PORT=8080
TORRENT_PORT=6881
MONITOR_PORT=61208
[[ -f .env ]] && {
    WEBUI_PORT=$(grep '^WEBUI_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 8080)
    TORRENT_PORT=$(grep '^TORRENT_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 6881)
    MONITOR_PORT=$(grep '^MONITOR_PORT=' .env 2>/dev/null | cut -d= -f2 || echo 61208)
}

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║     cyber-seed  —  firewall setup        ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""
info "Ports to open: TCP ${WEBUI_PORT} (qBittorrent), TCP ${MONITOR_PORT} (Glances), TCP+UDP ${TORRENT_PORT} (torrents)"
echo ""

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

# ── GCP (gcloud) ──────────────────────────────────────────────────────
if command -v gcloud &>/dev/null; then
    info "Detected GCP — using gcloud firewall rules ..."

    # Get current VM instance name and zone from metadata
    INSTANCE=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/name" \
        -H "Metadata-Flavor: Google" 2>/dev/null || hostname)
    ZONE=$(curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/zone" \
        -H "Metadata-Flavor: Google" 2>/dev/null | awk -F/ '{print $NF}' || echo "us-central1-c")

    # Create firewall rules (ignore error if already exist)
    gcloud compute firewall-rules create cyber-seed-webui \
        --network=default \
        --action=ALLOW \
        --rules="tcp:${WEBUI_PORT},tcp:${MONITOR_PORT}" \
        --source-ranges=0.0.0.0/0 \
        --target-tags=cyber-seed \
        --description="cyber-seed Web UIs" \
        --quiet 2>/dev/null \
        && success "Firewall rule cyber-seed-webui created." \
        || warn "Rule cyber-seed-webui may already exist — skipping."

    gcloud compute firewall-rules create cyber-seed-torrents \
        --network=default \
        --action=ALLOW \
        --rules="tcp:${TORRENT_PORT},udp:${TORRENT_PORT}" \
        --source-ranges=0.0.0.0/0 \
        --target-tags=cyber-seed \
        --description="cyber-seed torrent traffic" \
        --quiet 2>/dev/null \
        && success "Firewall rule cyber-seed-torrents created." \
        || warn "Rule cyber-seed-torrents may already exist — skipping."

    # Tag the VM
    gcloud compute instances add-tags "$INSTANCE" \
        --tags=cyber-seed \
        --zone="$ZONE" \
        --quiet 2>/dev/null \
        && success "Tag 'cyber-seed' added to instance $INSTANCE." \
        || warn "Could not add tag — add it manually in GCP Console."

# ── UFW (Ubuntu/Debian) ───────────────────────────────────────────────
elif command -v ufw &>/dev/null; then
    info "Detected ufw — opening ports ..."
    $SUDO ufw allow "${WEBUI_PORT}/tcp"    comment "cyber-seed qBittorrent"
    $SUDO ufw allow "${MONITOR_PORT}/tcp"  comment "cyber-seed Glances"
    $SUDO ufw allow "${TORRENT_PORT}/tcp"  comment "cyber-seed torrents TCP"
    $SUDO ufw allow "${TORRENT_PORT}/udp"  comment "cyber-seed torrents UDP"
    $SUDO ufw --force enable
    success "ufw rules applied."

# ── iptables (fallback) ───────────────────────────────────────────────
elif command -v iptables &>/dev/null; then
    info "Using iptables ..."
    for PORT in "$WEBUI_PORT" "$MONITOR_PORT" "$TORRENT_PORT"; do
        $SUDO iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null && \
            success "Opened TCP $PORT"
    done
    $SUDO iptables -I INPUT -p udp --dport "$TORRENT_PORT" -j ACCEPT 2>/dev/null && \
        success "Opened UDP $TORRENT_PORT"
    warn "iptables rules are not persistent. Install iptables-persistent to save them."

else
    warn "No supported firewall tool found (gcloud / ufw / iptables)."
    echo ""
    echo "  Open these ports manually in your cloud console or hosting panel:"
    echo "    TCP  ${WEBUI_PORT}    — qBittorrent Web UI"
    echo "    TCP  ${MONITOR_PORT}  — Glances monitor"
    echo "    TCP  ${TORRENT_PORT}  — Torrent traffic"
    echo "    UDP  ${TORRENT_PORT}  — Torrent traffic"
fi

echo ""
success "Firewall setup complete."
echo ""
PUBLIC_IP=$(curl -sf https://api.ipify.org 2>/dev/null || \
            curl -sf "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/externalIp" \
            -H "Metadata-Flavor: Google" 2>/dev/null || echo "<your-server-ip>")
echo "  Access your services at:"
echo -e "    qBittorrent → ${CYAN}http://${PUBLIC_IP}:${WEBUI_PORT}${NC}"
echo -e "    Glances     → ${CYAN}http://${PUBLIC_IP}:${MONITOR_PORT}${NC}"
echo ""
