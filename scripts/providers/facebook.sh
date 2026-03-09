#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: facebook
#  Handles: Facebook videos (public posts, reels, watch, stories)
#  Uses yt-dlp which has native Facebook support
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="facebook (yt-dlp)"

can_handle() {
    local url="$1"
    if echo "$url" | grep -qiE '(facebook\.com|fb\.watch|fb\.com)'; then
        return 0
    fi
    return 1
}

download() {
    local url="$1"
    local output_dir="$2"
    local log_file="${3:-/dev/stderr}"
    local fmt="${YT_FORMAT:-best}"

    echo "[facebook] Downloading: $url"
    echo "[facebook] Format: $fmt"
    echo "[facebook] Output dir: $output_dir"

    mkdir -p "$output_dir"

    # Facebook often needs cookies for non-public content;
    # for public videos yt-dlp works without them
    yt-dlp \
        --output "$output_dir/%(title)s.%(ext)s" \
        --format "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best" \
        --merge-output-format mp4 \
        --ignore-errors \
        --no-playlist \
        --retries 5 \
        --fragment-retries 5 \
        --concurrent-fragments 4 \
        --newline \
        --progress \
        "$url" 2>&1

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        echo "[facebook] Download complete."
    else
        echo "[facebook] ERROR: yt-dlp exited with code $exit_code"
    fi
    return $exit_code
}
