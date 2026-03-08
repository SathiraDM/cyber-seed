#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  on-complete.sh
#  Called by qBittorrent when a torrent finishes downloading.
#
#  qBittorrent passes these substitution variables:
#    %N  → Torrent name
#    %F  → Content path (file for single-file, folder for multi-file)
#    %D  → Save path (the download directory)
#    %L  → Category
#    %I  → Info hash
#
#  Configured in qBittorrent:
#    Tools > Options > Downloads > Run external program on torrent finish:
#    /scripts/on-complete.sh "%N" "%F" "%D"
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

TORRENT_NAME="${1:-unknown}"
CONTENT_PATH="${2:-}"
SAVE_PATH="${3:-}"

# ── Config (injected via docker-compose environment) ─────────────────
REMOTE="${ONEDRIVE_REMOTE:-onedrive}"
REMOTE_PATH="${ONEDRIVE_PATH:-/Torrents}"
RCLONE_CONF="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
LOG_FILE="/logs/upload.log"

# ── Helpers ───────────────────────────────────────────────────────────
mkdir -p /logs
timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { echo "[$(timestamp)] $*" | tee -a "$LOG_FILE"; }
fail() { log "ERROR: $*"; exit 1; }

log "═══════════════════════════════════════════════════"
log "TORRENT COMPLETE : $TORRENT_NAME"
log "Content path     : $CONTENT_PATH"
log "Save path        : $SAVE_PATH"
log "Destination      : ${REMOTE}:${REMOTE_PATH}/${TORRENT_NAME}"

# ── Sanity checks ────────────────────────────────────────────────────
[[ -z "$CONTENT_PATH" ]]  && fail "Content path is empty — check qBittorrent AutoRun args."
[[ ! -e "$CONTENT_PATH" ]] && fail "Content path does not exist: $CONTENT_PATH"
[[ ! -f "$RCLONE_CONF" ]] && fail "rclone config not found at: $RCLONE_CONF — run setup.sh first."

# ── Upload ────────────────────────────────────────────────────────────
log "Starting upload ..."

rclone copy "$CONTENT_PATH" \
    "${REMOTE}:${REMOTE_PATH}/${TORRENT_NAME}" \
    --config "$RCLONE_CONF" \
    --transfers=4 \
    --checkers=8 \
    --retries=3 \
    --low-level-retries=10 \
    --stats=30s \
    --stats-one-line \
    --log-file="$LOG_FILE" \
    --log-level INFO

RCLONE_EXIT=$?

# ── Result ────────────────────────────────────────────────────────────
if [[ $RCLONE_EXIT -eq 0 ]]; then
    log "UPLOAD SUCCESS   : $TORRENT_NAME → ${REMOTE}:${REMOTE_PATH}/${TORRENT_NAME}"

    # Delete local files to free disk space
    log "Removing local   : $CONTENT_PATH"
    rm -rf "$CONTENT_PATH"
    log "Local files deleted."
else
    log "UPLOAD FAILED    : $TORRENT_NAME (rclone exit $RCLONE_EXIT)"
    log "Files kept locally for retry. Check logs: $LOG_FILE"
    exit $RCLONE_EXIT
fi

log "═══════════════════════════════════════════════════"
