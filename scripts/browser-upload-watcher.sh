#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Browser Downloads → OneDrive Auto-Uploader
#  Watches /downloads/browser for completed files and uploads them
#  to OneDrive WebDownloads folder, then removes local copies.
#
#  Runs as a background loop, polling every 10 seconds.
#  Skips files that are still being written (size changing).
# ─────────────────────────────────────────────────────────────────────

set -u

WATCH_DIR="/downloads/browser"
REMOTE="${ONEDRIVE_REMOTE:-onedrive}"
REMOTE_PATH="${WEBDL_PATH:-/WebDownloads}"
RCLONE_CONF="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
LOG_FILE="/logs/browser-upload.log"
POLL_INTERVAL=10

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

mkdir -p "$WATCH_DIR" "$(dirname "$LOG_FILE")"
log "Browser upload watcher started"
log "Watching: $WATCH_DIR"
log "Uploading to: ${REMOTE}:${REMOTE_PATH}/"

while true; do
    # Find all files in the watch directory (non-recursive for now)
    shopt -s nullglob
    files=("$WATCH_DIR"/*)
    shopt -u nullglob

    for filepath in "${files[@]}"; do
        # Skip directories, temp files, and partial downloads
        [[ -d "$filepath" ]] && continue
        [[ "$filepath" == *.crdownload ]] && continue
        [[ "$filepath" == *.tmp ]] && continue
        [[ "$filepath" == *.part ]] && continue

        filename=$(basename "$filepath")

        # Check file is not still growing (wait and compare size)
        size1=$(stat -c%s "$filepath" 2>/dev/null || echo 0)
        sleep 2
        size2=$(stat -c%s "$filepath" 2>/dev/null || echo 0)

        if [[ "$size1" != "$size2" ]]; then
            # File is still being written
            continue
        fi

        if [[ "$size1" -eq 0 ]]; then
            # Empty file, skip
            continue
        fi

        log "New file detected: $filename ($(numfmt --to=iec "$size1" 2>/dev/null || echo "${size1} bytes"))"

        # Upload to OneDrive
        rclone copy "$filepath" \
            "${REMOTE}:${REMOTE_PATH}" \
            --config "$RCLONE_CONF" \
            --transfers=4 \
            --retries=3 \
            --low-level-retries=10 \
            --stats=5s \
            --stats-one-line \
            --verbose 2>&1 | tee -a "$LOG_FILE"

        up_exit=${PIPESTATUS[0]}

        if [[ $up_exit -eq 0 ]]; then
            log "UPLOAD SUCCESS: $filename → ${REMOTE}:${REMOTE_PATH}/"
            rm -f "$filepath"
            log "Removed local: $filename"
        else
            log "UPLOAD FAILED: $filename (rclone exit $up_exit) — will retry next cycle"
        fi
    done

    sleep "$POLL_INTERVAL"
done
