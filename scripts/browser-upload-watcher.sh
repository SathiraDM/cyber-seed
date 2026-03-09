#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Browser Downloads → OneDrive Auto-Uploader
#  Watches /downloads/browser for completed files, registers each as a
#  job in the web UI job index, then uploads via rclone to OneDrive.
#
#  Runs as a background service inside cyber-seed-qbt.
#  Polls every 10 seconds. Skips files still being written.
# ─────────────────────────────────────────────────────────────────────

set -u

WATCH_DIR="/downloads/browser"
REMOTE="${ONEDRIVE_REMOTE:-onedrive}"
REMOTE_PATH="${WEBDL_PATH:-/WebDownloads}"
RCLONE_CONF="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
JOBS_DIR="/logs/jobs"
POLL_INTERVAL=10

mkdir -p "$WATCH_DIR" "$JOBS_DIR"

log_job() {
    local job_id="$1"
    shift
    local msg="$*"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $msg" >> "$JOBS_DIR/${job_id}.log"
}

job_upsert() {
    # job_upsert <job_id> <filename> <status> [started_at] [ended_at]
    local job_id="$1" filename="$2" status="$3"
    local started_at="${4:-}"
    local ended_at="${5:-null}"
    python3 - "$job_id" "$filename" "$status" "$started_at" "$ended_at" <<'PYEOF'
import sys, json, pathlib, datetime

job_id, filename, status, started_at, ended_at = sys.argv[1:]
if not started_at:
    started_at = datetime.datetime.utcnow().isoformat()

index_path = pathlib.Path("/logs/jobs/index.json")
try:
    jobs = json.loads(index_path.read_text()) if index_path.exists() else {}
except Exception:
    jobs = {}

jobs[job_id] = {
    "id":         job_id,
    "url":        f"browser://{filename}",
    "name":       filename,
    "source":     "browser",
    "format":     "file",
    "status":     status,
    "started_at": started_at,
    "ended_at":   None if ended_at == "null" else ended_at,
}
index_path.write_text(json.dumps(jobs, indent=2))
PYEOF
}

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Browser upload watcher started (watching: $WATCH_DIR)"

while true; do
    shopt -s nullglob
    files=("$WATCH_DIR"/*)
    shopt -u nullglob

    for filepath in "${files[@]}"; do
        [[ -d "$filepath" ]] && continue
        [[ "$filepath" == *.crdownload ]] && continue
        [[ "$filepath" == *.tmp ]] && continue
        [[ "$filepath" == *.part ]] && continue

        filename=$(basename "$filepath")

        # Skip if already being processed (sentinel file)
        sentinel="$WATCH_DIR/.uploading_${filename}"
        [[ -f "$sentinel" ]] && continue

        # Check file is not still growing
        size1=$(stat -c%s "$filepath" 2>/dev/null || echo 0)
        sleep 3
        size2=$(stat -c%s "$filepath" 2>/dev/null || echo 0)
        [[ "$size1" != "$size2" || "$size1" -eq 0 ]] && continue

        # Claim the file
        touch "$sentinel"

        job_id=$(python3 -c "import uuid; print(str(uuid.uuid4())[:8])")
        started_at=$(python3 -c "import datetime; print(datetime.datetime.utcnow().isoformat())")
        size_human=$(numfmt --to=iec "$size1" 2>/dev/null || echo "${size1} bytes")

        job_upsert "$job_id" "$filename" "running" "$started_at" "null"
        log_job "$job_id" "File detected: $filename ($size_human)"
        log_job "$job_id" "Uploading to ${REMOTE}:${REMOTE_PATH}/ ..."

        rclone copy "$filepath" \
            "${REMOTE}:${REMOTE_PATH}" \
            --config "$RCLONE_CONF" \
            --transfers=4 \
            --retries=3 \
            --low-level-retries=10 \
            --stats=5s \
            --stats-one-line \
            --verbose >> "$JOBS_DIR/${job_id}.log" 2>&1

        up_exit=$?
        ended_at=$(python3 -c "import datetime; print(datetime.datetime.utcnow().isoformat())")

        if [[ $up_exit -eq 0 ]]; then
            job_upsert "$job_id" "$filename" "done" "$started_at" "$ended_at"
            log_job "$job_id" "✓ Upload complete → ${REMOTE}:${REMOTE_PATH}/${filename}"
            rm -f "$filepath"
            log_job "$job_id" "Local file removed."
        else
            job_upsert "$job_id" "$filename" "failed" "$started_at" "$ended_at"
            log_job "$job_id" "✗ Upload failed (rclone exit $up_exit) — file kept for retry."
        fi

        rm -f "$sentinel"
    done

    sleep "$POLL_INTERVAL"
done
