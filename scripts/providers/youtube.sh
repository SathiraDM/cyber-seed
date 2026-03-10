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

# ── Format selection ────────────────────────────────────────────────
# Pass YT_FORMAT env var to choose quality. Supported values:
#   best (default)  highest available video+audio
#   2160p           4K
#   1080p           Full HD
#   720p            HD
#   480p            SD
#   360p            Low
#   audio           Audio-only MP3

download() {
    local url="$1"
    local output_dir="$2"
    local log_file="${3:-/dev/stderr}"
    local fmt="${YT_FORMAT:-best}"
    local fmt_norm="${fmt%p}"  # normalise: strip trailing 'p' so "1080p" and "1080" both work

    echo "[youtube] Downloading: $url"
    echo "[youtube] Format: $fmt"
    echo "[youtube] Output dir: $output_dir"

    mkdir -p "$output_dir"

    # Build format flags as an array to avoid word-splitting issues
    local -a fmt_flags
    case "$fmt_norm" in
        2160) fmt_flags=(--format 'bestvideo[height<=2160][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=2160]+bestaudio/best' --merge-output-format mp4) ;;
        1080) fmt_flags=(--format 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best' --merge-output-format mp4) ;;
        720)  fmt_flags=(--format 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best'   --merge-output-format mp4) ;;
        480)  fmt_flags=(--format 'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best'   --merge-output-format mp4) ;;
        360)  fmt_flags=(--format 'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best'   --merge-output-format mp4) ;;
        audio) fmt_flags=(--format 'bestaudio/best' --extract-audio --audio-format mp3 --audio-quality 0) ;;
        *)     fmt_flags=(--format 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best' --merge-output-format mp4) ;;
    esac

    # Include quality tag in filename for fixed-quality video downloads
    local quality_suffix=""
    case "$fmt_norm" in
        2160|1080|720|480|360) quality_suffix=" [${fmt_norm}p]" ;;
    esac

    yt-dlp \
        --output "$output_dir/%(title)s${quality_suffix}.%(ext)s" \
        "${fmt_flags[@]}" \
        --trim-filenames 200 \
        --embed-subs \
        --sub-langs "en.*,en" \
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
        echo "[youtube] Download complete ($fmt)."
    else
        echo "[youtube] ERROR: yt-dlp exited with code $exit_code"
    fi
    return $exit_code
}
