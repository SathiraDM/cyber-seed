# cyber-seed — qBittorrent + rclone + yt-dlp + aria2 in one image
# Base: linuxserver/qbittorrent (Alpine-based, LSIO-managed)
FROM lscr.io/linuxserver/qbittorrent:latest

# Install rclone, yt-dlp, aria2 (fast multi-connection downloader), ffmpeg (yt-dlp muxing)
RUN apk add --no-cache curl bash unzip python3 py3-pip aria2 ffmpeg py3-cryptography && \
    curl https://rclone.org/install.sh | bash && \
    pip3 install --no-cache-dir --break-system-packages yt-dlp && \
    rm -rf /var/cache/apk/*

# Ensure scripts directory exists with correct permissions
RUN mkdir -p /scripts /scripts/providers /logs

# Default entrypoint/cmd inherited from base image (s6-overlay)
