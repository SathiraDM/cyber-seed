#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: noodlemagazine
#  Handles: noodlemagazine.com videos
#  Uses yt-dlp which has a native NoodleMagazine extractor
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="noodlemagazine (yt-dlp)"

can_handle() {
    local url="$1"
    if echo "$url" | grep -qiE 'noodlemagazine\.com'; then
        return 0
    fi
    return 1
}

download() {
    local url="$1"
    local output_dir="$2"
    local log_file="${3:-/dev/stderr}"
    local fmt="${YT_FORMAT:-best}"

    echo "[noodlemagazine] Downloading: $url" >> "$log_file"
    echo "[noodlemagazine] Format: $fmt" >> "$log_file"
    echo "[noodlemagazine] Output dir: $output_dir" >> "$log_file"

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
        echo "[noodlemagazine] Download complete." >> "$log_file"
    else
        echo "[noodlemagazine] ERROR: yt-dlp exited with code $exit_code" >> "$log_file"
    fi
    return $exit_code
}
