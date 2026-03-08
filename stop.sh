#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  stop.sh  —  Gracefully stop the cyber-seed stack
#  Active downloads will pause and resume on next start.
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; NC='\033[0m'

echo -e "${CYAN}[cyber-seed]${NC} Stopping stack ..."
docker compose down
echo -e "${GREEN}Stack stopped.${NC} Downloads are paused and will resume on ./start.sh"
