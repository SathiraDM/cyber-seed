#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: youtube
#  Handles: YouTube + 1000s of sites supported by yt-dlp
#  (dailymotion, vimeo, twitter/x, instagram, tiktok, reddit, etc.)
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="youtube (yt-dlp)"

can_handle() {
    local url="$1"
    # yt-dlp can probe any URL — let it decide
    # But we want to be first for known video sites, and fallback otherwise
    # Check against known video hosting domains
    if echo "$url" | grep -qiE \
        '(youtube\.com|youtu\.be|vimeo\.com|dailymotion\.com|twitch\.tv|twitter\.com|x\.com|instagram\.com|tiktok\.com|reddit\.com|bilibili\.com|niconico\.jp|pornhub\.com|xvideos\.com|xhamster\.com|iwara\.tv|spankbang\.com|eporner\.com|thisvid\.com)'; then
        return 0
    fi
    # Also try with yt-dlp --simulate to check if it can handle any other URL
    # (more expensive but catches everything else)
    if yt-dlp --simulate --quiet --no-warnings "$url" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

download() {
    local url="$1"
    local output_dir="$2"
    local log_file="${3:-/dev/stderr}"

    echo "[youtube] Downloading: $url" >> "$log_file"
    echo "[youtube] Output dir: $output_dir" >> "$log_file"

    mkdir -p "$output_dir"

    yt-dlp \
        --output "$output_dir/%(title)s.%(ext)s" \
        --format "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best" \
        --merge-output-format mp4 \
        --write-info-json \
        --write-thumbnail \
        --embed-subs \
        --sub-langs "en.*,en" \
        --ignore-errors \
        --no-playlist \
        --retries 5 \
        --fragment-retries 5 \
        --concurrent-fragments 4 \
        --newline \
        --progress \
        "$url" >> "$log_file" 2>&1

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        echo "[youtube] Download complete." >> "$log_file"
    else
        echo "[youtube] ERROR: yt-dlp exited with code $exit_code" >> "$log_file"
    fi
    return $exit_code
}
