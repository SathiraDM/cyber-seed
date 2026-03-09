#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  Provider: faphouse
#  Handles: faphouse.com videos (requires authentication)
#
#  Uses CDP (Chrome DevTools Protocol) to intercept the real signed video URL
#  from the browser container, then downloads it with yt-dlp.
#
#  Requirements:
#    - cyber-seed-browser container running with remote debugging enabled
#      (CHROMIUM_EXTRA_PARAMS in docker-compose.yml)
#    - /config/cookies/faphouse-cookies.txt — export from browser via
#      'Get cookies.txt LOCALLY' extension, re-export when session expires
#
#  Interface (called by download-url.sh):
#    can_handle <url>   → exit 0 if this provider handles the URL
#    download <url> <output_dir> <log_file>  → download into output_dir
# ─────────────────────────────────────────────────────────────────────

PROVIDER_NAME="faphouse (yt-dlp + CDP)"
COOKIES_FILE="/config/cookies/faphouse-cookies.txt"

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
        echo "[faphouse] Export cookies from faphouse.com using 'Get cookies.txt LOCALLY' extension,"
        echo "[faphouse] then: docker cp faphouse-cookies.txt cyber-seed-qbt:/config/cookies/faphouse-cookies.txt"
        exit 1
    fi

    echo "[faphouse] Using cookies from: $COOKIES_FILE"
    mkdir -p "$output_dir"

    # Strip URL fragment (#...) — causes encoding issues
    local clean_url="${url%%#*}"
    echo "[faphouse] Page URL: $clean_url"

    # Map YT_FORMAT to a quality number for find-video-url.py
    local quality
    case "$fmt" in
        2160p) quality="2160" ;;
        1080p) quality="1080" ;;
        720p)  quality="720"  ;;
        480p)  quality="480"  ;;
        360p)  quality="360"  ;;
        *)     quality="1080" ;;  # best/default
    esac

    # Use find-video-url.py to get the direct signed video URL
    echo "[faphouse] Extracting direct video URL (quality=${quality}p)..."
    local video_url
    video_url=$(python3 /scripts/find-video-url.py "$clean_url" "$quality" 2>/dev/null)
    local find_exit=$?

    if [[ $find_exit -ne 0 || -z "$video_url" ]]; then
        echo "[faphouse] WARNING: Could not get direct URL, falling back to page URL..."
        video_url="$clean_url"
    else
        echo "[faphouse] Direct video URL: $video_url"
    fi

    # For audio-only format, still use yt-dlp format selection
    local -a fmt_flags
    if [[ "$fmt" == "audio" ]]; then
        fmt_flags=(--format 'bestaudio/best' --extract-audio --audio-format mp3 --audio-quality 0)
    else
        # URL is already the right quality (direct mp4 or quality-specific m3u8)
        fmt_flags=(--format 'best')
    fi

    yt-dlp \
        --output "$output_dir/%(title)s.%(ext)s" \
        "${fmt_flags[@]}" \
        --cookies "$COOKIES_FILE" \
        --trim-filenames 200 \
        --retries 5 \
        --fragment-retries 5 \
        --concurrent-fragments 4 \
        --newline \
        --progress \
        --add-header "Referer:https://faphouse.com" \
        --add-header "Origin:https://faphouse.com" \
        "$video_url" 2>&1

    local exit_code=$?
    if [[ $exit_code -eq 0 ]]; then
        echo "[faphouse] Download complete."
    else
        echo "[faphouse] yt-dlp exited with code $exit_code."
        exit $exit_code
    fi
}
