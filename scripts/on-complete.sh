#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  on-complete.sh
#  Called by qBittorrent when a torrent finishes downloading.
#
#  qBittorrent passes these substitution variables:
#    %N  → Torrent name
#    %F  → Content path (file for single-file, folder for multi-file)
#    %D  → Save path (the download directory)
#    %I  → Info hash (used to remove torrent via API after upload)
#    %L  → Category
#
#  Configured in qBittorrent:
#    Tools > Options > Downloads > Run external program on torrent finish:
#    /bin/bash /scripts/on-complete.sh "%N" "%F" "%D" "%I"
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

TORRENT_NAME="${1:-unknown}"
CONTENT_PATH="${2:-}"
SAVE_PATH="${3:-}"
TORRENT_HASH="${4:-}"

# ── Config (injected via docker-compose environment) ─────────────────
REMOTE="${ONEDRIVE_REMOTE:-onedrive}"
REMOTE_PATH="${ONEDRIVE_PATH:-/Torrents}"
RCLONE_CONF="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
QBT_URL="http://localhost:8080"
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

    # Remove torrent from qBittorrent via API (login required)
    if [[ -n "$TORRENT_HASH" ]]; then
        # Try multiple sources for the password
        QBT_PASS="${QBT_WEBUI_PASS:-}"
        [[ -z "$QBT_PASS" ]] && QBT_PASS=$(cat /config/.qbt_pass 2>/dev/null | tr -d '\r\n')
        QBT_PASS="${QBT_PASS:-adminadmin}"
        log "qBittorrent API: logging in (pass length: ${#QBT_PASS})"

        QBT_COOKIE=$(mktemp)
        LOGIN=$(curl -sf --max-time 10 \
            -c "$QBT_COOKIE" \
            --data "username=admin&password=${QBT_PASS}" \
            "${QBT_URL}/api/v2/auth/login" 2>&1)
        log "qBittorrent login: $LOGIN"
        if [[ "$LOGIN" == "Ok." ]]; then
            DELETE_RESP=$(curl -s --max-time 10 \
                -b "$QBT_COOKIE" \
                --data "hashes=${TORRENT_HASH}&deleteFiles=false" \
                "${QBT_URL}/api/v2/torrents/delete" 2>&1)
            DELETE_EXIT=$?
            if [[ $DELETE_EXIT -eq 0 ]]; then
                log "Torrent removed from qBittorrent: $TORRENT_HASH"
            else
                log "WARN: Delete call failed (exit $DELETE_EXIT): $DELETE_RESP"
            fi
        else
            log "WARN: qBittorrent login failed ('$LOGIN') — torrent not removed."
        fi
        rm -f "$QBT_COOKIE"
    fi
else
    log "UPLOAD FAILED    : $TORRENT_NAME (rclone exit $RCLONE_EXIT)"
    log "Files kept locally for retry. Check logs: $LOG_FILE"
    exit $RCLONE_EXIT
fi

log "═══════════════════════════════════════════════════"
