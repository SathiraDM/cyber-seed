#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: faphouse
#  Handles: faphouse.com videos (requires authentication)
#
#  Uses yt-dlp --cookies-from-browser which reads directly from the
#  Chromium profile in the browser container (no manual export needed).
#  Just log in to the site in the virtual browser first.
#
#  Browser profile location (mounted in qbt container):
#    /config/browser/chromium
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="faphouse (yt-dlp + browser cookies)"
BROWSER_PROFILE="/config/browser/chromium"

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

    if [[ ! -d "$BROWSER_PROFILE" ]]; then
        echo "[faphouse] ERROR: Browser profile not found at $BROWSER_PROFILE"
        echo "[faphouse] Make sure the virtual browser has been opened at least once."
        exit 1
    fi

    echo "[faphouse] Reading cookies from browser profile: $BROWSER_PROFILE"
    mkdir -p "$output_dir"

    # Strip URL fragment (#...) — causes encoding issues
    local clean_url="${url%%#*}"
    echo "[faphouse] Page URL: $clean_url"

    # Find the real direct video/stream URL on the page
    echo "[faphouse] Searching for real video URL on page..."
    local video_url
    video_url=$(python3 /scripts/find-video-url.py "$clean_url" 2>&1 | tee /dev/stderr | tail -1)

    # Re-run cleanly capturing only stdout (the URL)
    video_url=$(python3 /scripts/find-video-url.py "$clean_url" 2>/dev/null)

    if [[ -z "$video_url" ]]; then
        echo "[faphouse] WARNING: Could not find direct video URL, falling back to page URL with yt-dlp..."
        video_url="$clean_url"
    else
        echo "[faphouse] Direct video URL: $video_url"
    fi

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
        --cookies-from-browser "chromium:$BROWSER_PROFILE" \
        --trim-filenames 200 \
        --no-playlist \
        --retries 5 \
        --fragment-retries 5 \
        --concurrent-fragments 4 \
        --newline \
        --progress \
        --add-header "Referer:https://faphouse.com" \
        "$video_url" 2>&1

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        echo "[faphouse] Download complete."
    else
        echo "[faphouse] yt-dlp exited with code $exit_code."
        exit $exit_code
    fi
}
