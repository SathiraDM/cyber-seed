#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Browser Downloads → OneDrive Auto-Uploader
#  Watches /downloads/browser and /downloads/faphouse for completed
#  files, registers each as a job in the web UI job index, then
#  uploads via rclone to OneDrive.
#
#  Runs as a background service inside cyber-seed-qbt.
#  Polls every 10 seconds. Skips files still being written.
# ─────────────────────────────────────────────────────────────────────

set -u

REMOTE="${ONEDRIVE_REMOTE:-onedrive}"
WEBDL_BASE="${WEBDL_PATH:-/WebDownloads}"
RCLONE_CONF="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
JOBS_DIR="/logs/jobs"
POLL_INTERVAL=10

mkdir -p /downloads/browser /downloads/faphouse "$JOBS_DIR"

log_job() {
    local job_id="$1"; shift
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$JOBS_DIR/${job_id}.log"
}

job_upsert() {
    # job_upsert <job_id> <filename> <status> [started_at] [ended_at] [source]
    local job_id="$1" filename="$2" status="$3"
    local started_at="${4:-}" ended_at="${5:-null}" source="${6:-browser}"
    python3 - "$job_id" "$filename" "$status" "$started_at" "$ended_at" "$source" <<'PYEOF'
import sys, json, pathlib, datetime

job_id, filename, status, started_at, ended_at, source = sys.argv[1:]
if not started_at:
    started_at = datetime.datetime.utcnow().isoformat()

index_path = pathlib.Path("/logs/jobs/index.json")
try:
    jobs = json.loads(index_path.read_text()) if index_path.exists() else {}
except Exception:
    jobs = {}

jobs[job_id] = {
    "id":         job_id,
    "url":        f"{source}://{filename}",
    "name":       filename,
    "source":     source,
    "format":     "file",
    "status":     status,
    "started_at": started_at,
    "ended_at":   None if ended_at == "null" else ended_at,
}
index_path.write_text(json.dumps(jobs, indent=2))
PYEOF
}

# ── Process one watch directory ──────────────────────────────────────
process_dir() {
    local watch_dir="$1"
    local remote_path="$2"
    local source_tag="$3"   # e.g. "browser" or "faphouse"

    shopt -s nullglob
    local files=("$watch_dir"/*)
    shopt -u nullglob

    for filepath in "${files[@]}"; do
        [[ -d "$filepath" ]] && continue
        [[ "$filepath" == *.crdownload ]] && continue
        [[ "$filepath" == *.tmp ]]        && continue
        [[ "$filepath" == *.part ]]       && continue
        [[ "$filepath" == *.aria2 ]]      && continue   # aria2 control files

        local filename
        filename=$(basename "$filepath")

        # Skip if already being processed (sentinel file)
        local sentinel="$watch_dir/.uploading_${filename}"
        [[ -f "$sentinel" ]] && continue

        # Check file is not still growing
        local size1 size2
        size1=$(stat -c%s "$filepath" 2>/dev/null || echo 0)
        sleep 3
        size2=$(stat -c%s "$filepath" 2>/dev/null || echo 0)
        [[ "$size1" != "$size2" || "$size1" -eq 0 ]] && continue

        # Claim the file
        touch "$sentinel"

        local job_id started_at size_human
        job_id=$(python3 -c "import uuid; print(str(uuid.uuid4())[:8])")
        started_at=$(python3 -c "import datetime; print(datetime.datetime.utcnow().isoformat())")
        size_human=$(numfmt --to=iec "$size1" 2>/dev/null || echo "${size1} bytes")

        job_upsert "$job_id" "$filename" "running" "$started_at" "null" "$source_tag"
        log_job "$job_id" "File detected: $filename ($size_human)"
        log_job "$job_id" "Uploading to ${REMOTE}:${remote_path}/ ..."

        rclone copy "$filepath" \
            "${REMOTE}:${remote_path}" \
            --config "$RCLONE_CONF" \
            --transfers=4 \
            --retries=3 \
            --low-level-retries=10 \
            --stats=5s \
            --stats-one-line \
            --verbose >> "$JOBS_DIR/${job_id}.log" 2>&1

        local up_exit=$?
        local ended_at
        ended_at=$(python3 -c "import datetime; print(datetime.datetime.utcnow().isoformat())")

        if [[ $up_exit -eq 0 ]]; then
            job_upsert "$job_id" "$filename" "done" "$started_at" "$ended_at" "$source_tag"
            log_job "$job_id" "✓ Upload complete → ${REMOTE}:${remote_path}/${filename}"
            rm -f "$filepath"
            log_job "$job_id" "Local file removed."
        else
            job_upsert "$job_id" "$filename" "failed" "$started_at" "$ended_at" "$source_tag"
            log_job "$job_id" "✗ Upload failed (rclone exit $up_exit) — file kept for retry."
        fi

        rm -f "$sentinel"
    done
}

# ── Main loop ────────────────────────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Browser upload watcher started"
echo "[$(date '+%Y-%m-%d %H:%M:%S')]   /downloads/browser    → ${REMOTE}:${WEBDL_BASE}"
echo "[$(date '+%Y-%m-%d %H:%M:%S')]   /downloads/faphouse   → ${REMOTE}:${WEBDL_BASE}/faphouse"

while true; do
    process_dir "/downloads/browser"  "${WEBDL_BASE}"           "browser"
    process_dir "/downloads/faphouse" "${WEBDL_BASE}/faphouse"  "faphouse"
    sleep "$POLL_INTERVAL"
done