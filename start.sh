#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  start.sh  —  Launch the cyber-seed stack
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo -e "${CYAN}[cyber-seed]${NC} Starting stack ..."

# Sanity checks
[[ ! -f ".env" ]] && echo -e "${YELLOW}[WARN]${NC} .env not found. Run ./setup.sh first." && exit 1
[[ ! -f "config/rclone/rclone.conf" ]] && echo -e "${YELLOW}[WARN]${NC} rclone.conf not found. Run ./setup.sh first." && exit 1

# Build the custom image (only rebuilds if Dockerfile changed)
docker compose build --quiet

# Launch
docker compose up -d

# Wait a moment then show status
sleep 2
docker compose ps

# Get ports from .env
WEBUI_PORT=$(grep '^WEBUI_PORT=' .env | cut -d= -f2)
MONITOR_PORT=$(grep '^MONITOR_PORT=' .env | cut -d= -f2)
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo ""
echo -e "${GREEN}Stack is up!${NC}"
echo -e "  qBittorrent → ${CYAN}http://${HOST_IP}:${WEBUI_PORT:-8080}${NC}"
echo -e "  Disk/System → ${CYAN}http://${HOST_IP}:${MONITOR_PORT:-61208}${NC}  (Glances)"
echo -e "  Logs        → ${CYAN}./logs/upload.log${NC}  (tailed with: ${YELLOW}./logs.sh${NC})"
echo ""
echo -e "  Default login: ${YELLOW}admin / adminadmin${NC}  ← change immediately!"
echo ""
