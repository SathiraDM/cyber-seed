#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: faphouse
#  Handles: faphouse.com videos (requires authentication cookies)
#
#  Uses yt-dlp generic extractor with a Netscape cookies file.
#  Export cookies first by clicking "Export Cookies" in CyberSeed
#  after logging in to faphouse.com in the virtual browser.
#
#  Cookies file location (in qbt container):
#    /config/rclone/faphouse-cookies.txt
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="faphouse (yt-dlp + cookies)"
COOKIES_FILE="/config/rclone/faphouse-cookies.txt"

can_handle() {
    local url="$1"
    if echo "$url" | grep -qiE 'faphouse\.com'; then
        return 0
    fi
    return 1
}

download() {
    local url="$1"
    local output_dir="$2"
    local log_file="${3:-/dev/stderr}"
    local fmt="${YT_FORMAT:-best}"

    echo "[faphouse] Downloading: $url"
    echo "[faphouse] Format: $fmt"
    echo "[faphouse] Output dir: $output_dir"

    if [[ ! -f "$COOKIES_FILE" ]]; then
        echo "[faphouse] ERROR: Cookies file not found at $COOKIES_FILE"
        echo "[faphouse] Please log in to faphouse.com in the virtual browser,"
        echo "[faphouse] then click 'Export Faphouse Cookies' in CyberSeed."
        exit 1
    fi

    echo "[faphouse] Using cookies file: $COOKIES_FILE"
    mkdir -p "$output_dir"

    local -a fmt_flags
    case "$fmt" in
        2160p) fmt_flags=(--format 'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best' --merge-output-format mp4) ;;
        1080p) fmt_flags=(--format 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best' --merge-output-format mp4) ;;
        720p)  fmt_flags=(--format 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best'   --merge-output-format mp4) ;;
        480p)  fmt_flags=(--format 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best'   --merge-output-format mp4) ;;
        360p)  fmt_flags=(--format 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best'   --merge-output-format mp4) ;;
        audio) fmt_flags=(--format 'bestaudio/best' --extract-audio --audio-format mp3 --audio-quality 0) ;;
        *)     fmt_flags=(--format 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best' --merge-output-format mp4) ;;
    esac

    yt-dlp \
        --output "$output_dir/%(title)s.%(ext)s" \
        "${fmt_flags[@]}" \
        --cookies "$COOKIES_FILE" \
        --trim-filenames 200 \
        --ignore-errors \
        --no-playlist \
        --retries 5 \
        --fragment-retries 5 \
        --concurrent-fragments 4 \
        --newline \
        --progress \
        --add-header "Referer:https://faphouse.com" \
        "$url" 2>&1

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        echo "[faphouse] Download complete."
    else
        echo "[faphouse] yt-dlp exited with code $exit_code."
        exit $exit_code
    fi
}
