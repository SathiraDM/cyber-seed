#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: direct
#  Handles: Plain HTTP/HTTPS direct download links
#  Uses aria2c (multi-connection) for speed, falls back to curl
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="direct (aria2c)"

can_handle() {
    local url="$1"
    # Handle any http/https URL as a last resort
    if echo "$url" | grep -qiE '^https?://'; then
        return 0
    fi
    return 1
}

download() {
    local url="$1"
    local output_dir="$2"
    local log_file="${3:-/dev/stderr}"

    echo "[direct] Downloading: $url" >> "$log_file"
    echo "[direct] Output dir: $output_dir" >> "$log_file"

    mkdir -p "$output_dir"

    # Derive filename from URL (last path segment, URL-decoded)
    local filename
    filename=$(basename "$(echo "$url" | cut -d'?' -f1)" | python3 -c "
import sys, urllib.parse
print(urllib.parse.unquote(sys.stdin.read().strip()))
")
    [[ -z "$filename" || "$filename" == "/" ]] && filename="download"

    echo "[direct] Filename: $filename" >> "$log_file"

    if command -v aria2c >/dev/null 2>&1; then
        aria2c \
            --dir="$output_dir" \
            --out="$filename" \
            --max-connection-per-server=16 \
            --split=16 \
            --min-split-size=1M \
            --max-tries=5 \
            --retry-wait=3 \
            --console-log-level=notice \
            --summary-interval=30 \
            --log="$log_file" \
            --log-level=notice \
            "$url"
    else
        echo "[direct] aria2c not found, falling back to curl" >> "$log_file"
        curl -L \
            --output "$output_dir/$filename" \
            --retry 5 \
            --retry-delay 3 \
            --progress-bar \
            "$url" >> "$log_file" 2>&1
    fi

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        echo "[direct] Download complete: $output_dir/$filename" >> "$log_file"
    else
        echo "[direct] ERROR: Download failed with exit code $exit_code" >> "$log_file"
    fi
    return $exit_code
}
