#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  download-url.sh  —  Multi-source downloader → OneDrive uploader
#
#  Usage:
#    ./download-url.sh <url> [output-name]
#    ./download-url.sh --file urls.txt        ← one URL per line
#
#  Providers are loaded from /scripts/providers/*.sh
#  Add a new provider by dropping a .sh file in that folder with:
#    can_handle <url>  → exit 0 if supported
#    download <url> <output_dir> <log_file>
#
#  Environment (from docker-compose):
#    ONEDRIVE_REMOTE, WEBDL_PATH, RCLONE_CONFIG
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────
REMOTE="${ONEDRIVE_REMOTE:-onedrive}"
REMOTE_PATH="${WEBDL_PATH:-/WebDownloads}"
RCLONE_CONF="${RCLONE_CONFIG:-/config/rclone/rclone.conf}"
DOWNLOADS_BASE="/downloads"
LOG_FILE="/logs/download-url.log"
PROVIDERS_DIR="/scripts/providers"

# ── Helpers ───────────────────────────────────────────────────────────
mkdir -p /logs
timestamp() { date '+%Y-%m-%d %H:%M:%S'; }
log()  { echo "[$(timestamp)] $*" | tee -a "$LOG_FILE"; }
fail() { log "ERROR: $*"; exit 1; }
sep()  { log "═══════════════════════════════════════════════════"; }

usage() {
    echo "Usage:"
    echo "  $0 <url> [output-name]"
    echo "  $0 --file urls.txt"
    echo ""
    echo "Examples:"
    echo "  $0 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'"
    echo "  $0 'https://example.com/file.zip' MyFile"
    echo "  $0 --file /tmp/my-urls.txt"
    exit 1
}

# ── Load providers (order matters — first match wins) ─────────────────
# Order: youtube first (most specific), direct last (catch-all)
PROVIDER_FILES=()
# Load named providers in priority order
for p in youtube facebook noodlemagazine direct; do
    f="$PROVIDERS_DIR/${p}.sh"
    [[ -f "$f" ]] && PROVIDER_FILES+=("$f")
done
# Load any extra providers not in the priority list
for f in "$PROVIDERS_DIR"/*.sh; do
    [[ -f "$f" ]] || continue
    basename="${f##*/}"
    basename="${basename%.sh}"
    [[ "$basename" == "youtube" || "$basename" == "facebook" || "$basename" == "noodlemagazine" || "$basename" == "direct" ]] && continue
    PROVIDER_FILES+=("$f")
done

# ── Find provider for a URL ───────────────────────────────────────────
find_provider() {
    local url="$1"
    # Honour manual override — FORCE_PROVIDER=youtube/facebook/direct/etc.
    if [[ -n "${FORCE_PROVIDER:-}" ]]; then
        local forced="$PROVIDERS_DIR/${FORCE_PROVIDER}.sh"
        if [[ -f "$forced" ]]; then
            echo "$forced"
            return 0
        else
            echo "[$(timestamp)] WARNING: FORCE_PROVIDER='${FORCE_PROVIDER}' not found at $forced, falling back to auto-detect" | tee -a "$LOG_FILE" >&2
        fi
    fi
    for provider_file in "${PROVIDER_FILES[@]}"; do
        (
            source "$provider_file"
            can_handle "$url"
        ) && echo "$provider_file" && return 0
    done
    return 1
}

# ── Upload via rclone ─────────────────────────────────────────────────
upload_to_onedrive() {
    local local_path="$1"
    local name="$2"

    log "Uploading → ${REMOTE}:${REMOTE_PATH}/"

    rclone copy "$local_path" \
        "${REMOTE}:${REMOTE_PATH}" \
        --config "$RCLONE_CONF" \
        --exclude '*.json' \
        --exclude '*.webp' \
        --exclude '*.jpg.webp' \
        --exclude '*.png.webp' \
        --transfers=4 \
        --checkers=8 \
        --retries=3 \
        --low-level-retries=10 \
        --stats=5s \
        --stats-one-line \
        --verbose \
        --use-json-log 2>&1

    return $?
}

# ── Process a single URL ──────────────────────────────────────────────
process_url() {
    local url="$1"
    local output_name="${2:-}"

    sep
    log "URL        : $url"

    # Derive output name from URL if not given
    if [[ -z "$output_name" ]]; then
        output_name=$(echo "$url" | python3 -c "
import sys, urllib.parse, re

# Generic path segments that carry no useful name info
GENERIC = {'watch','video','videos','reel','reels','shorts','embed',
           'v','w','clip','clips','stream','live','post','photo',
           'status','p','t','e'}

u = sys.stdin.read().strip()
parsed = urllib.parse.urlparse(u)
qs     = urllib.parse.parse_qs(parsed.query)

# Try path last segment first
path = parsed.path.rstrip('/')
seg  = path.split('/')[-1] if path else ''
seg  = urllib.parse.unquote(seg)
seg  = re.sub(r'\.[a-zA-Z0-9]{1,5}$', '', seg)  # strip extension

if seg.lower() in GENERIC or len(seg) <= 2:
    # Fall back: look for common video-id query params
    for param in ('v', 'id', 'video_id', 'vid', 'clip_id'):
        val = qs.get(param, [None])[0]
        if val:
            seg = val
            break
    else:
        # Last resort: use sanitised hostname + first meaningful path part
        host  = parsed.hostname or 'download'
        parts = [p for p in path.split('/') if p and p.lower() not in GENERIC]
        seg   = (parts[0] if parts else host)[:40]

name = re.sub(r'[^\w\s\-\.]', '_', seg).strip('_').strip()
print(name[:100] or 'download')
")
    fi

    log "Name       : $output_name"

    # Find provider
    local provider_file
    provider_file=$(find_provider "$url") || fail "No provider found for URL: $url"
    log "Provider   : $(basename "$provider_file" .sh)"

    # Download
    local output_dir="${DOWNLOADS_BASE}/${output_name}"
    mkdir -p "$output_dir"

    # Disable errexit so a non-zero exit from the provider doesn't
    # get swallowed — we capture it explicitly below
    set +e
    (
        set +e
        source "$provider_file"
        download "$url" "$output_dir" "$LOG_FILE"
    )
    local dl_exit=$?
    set -e

    if [[ $dl_exit -ne 0 ]]; then
        log "DOWNLOAD FAILED: $url (exit $dl_exit)"
        rm -rf "$output_dir"
        return $dl_exit
    fi

    log "DOWNLOAD OK: $output_dir"

    # Check rclone config exists
    [[ ! -f "$RCLONE_CONF" ]] && fail "rclone config not found: $RCLONE_CONF"

    # Upload
    upload_to_onedrive "$output_dir" "$output_name"
    local up_exit=$?

    if [[ $up_exit -eq 0 ]]; then
        log "UPLOAD SUCCESS: $output_name → ${REMOTE}:${REMOTE_PATH}/"
    else
        log "UPLOAD FAILED: $output_name (rclone exit $up_exit)"
    fi

    log "Removing local: $output_dir"
    rm -rf "$output_dir"
    log "Done."

    return $up_exit

    sep
}

# ── Main ──────────────────────────────────────────────────────────────
[[ $# -eq 0 ]] && usage

if [[ "${1:-}" == "--file" ]]; then
    url_file="${2:-}"
    [[ -z "$url_file" ]] && fail "--file requires a path argument"
    [[ ! -f "$url_file" ]] && fail "URL file not found: $url_file"

    log "Processing URL file: $url_file"
    success=0
    failed=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        # Support "url name" format (tab or space separated)
        url=$(echo "$line" | awk '{print $1}')
        name=$(echo "$line" | awk '{$1=""; print $0}' | xargs)
        if process_url "$url" "$name"; then
            ((success++))
        else
            ((failed++))
        fi
    done < "$url_file"

    log "Batch complete: $success succeeded, $failed failed."
else
    url="$1"
    name="${2:-}"
    process_url "$url" "$name"
fi
