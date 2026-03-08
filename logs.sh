#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  logs.sh  —  Live tail of container logs + upload log
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'

case "${1:-all}" in
    qbt|qbittorrent)
        echo -e "${CYAN}[qBittorrent container logs]${NC} (Ctrl+C to exit)"
        docker compose logs -f qbittorrent
        ;;
    upload)
        echo -e "${CYAN}[Upload log]${NC} (Ctrl+C to exit)"
        tail -f logs/upload.log 2>/dev/null || echo -e "${YELLOW}No upload log yet.${NC}"
        ;;
    all|*)
        echo -e "${CYAN}[All logs — split pane]${NC}"
        echo "Usage:"
        echo "  ./logs.sh qbt      — qBittorrent container logs"
        echo "  ./logs.sh upload   — rclone upload log (logs/upload.log)"
        echo ""
        echo "Showing container logs (Ctrl+C to exit):"
        docker compose logs -f
        ;;
esac
