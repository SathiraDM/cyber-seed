# cyber-seed — qBittorrent + rclone in one image
# Base: linuxserver/qbittorrent (Alpine-based, LSIO-managed)
FROM lscr.io/linuxserver/qbittorrent:latest

# Install rclone + bash (Alpine uses sh by default)
# rclone install script handles arch detection automatically
RUN apk add --no-cache curl bash unzip && \
    curl https://rclone.org/install.sh | bash && \
    rm -rf /var/cache/apk/*

# Ensure scripts directory exists with correct permissions
RUN mkdir -p /scripts /logs

# Default entrypoint/cmd inherited from base image (s6-overlay)
