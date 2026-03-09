#!/usr/bin/with-contenv bash
# Launched by lsio /custom-cont-init.d at container startup.
# Forks the upload watcher as a background daemon.

WATCHER="/scripts/browser-upload-watcher.sh"

if [[ ! -x "$WATCHER" ]]; then
    echo "[browser-watcher-launch] $WATCHER not found or not executable, skipping"
    exit 0
fi

# Kill any existing instance
pkill -f browser-upload-watcher.sh 2>/dev/null || true

nohup "$WATCHER" >> /logs/browser-upload.log 2>&1 &
echo "[browser-watcher-launch] Started watcher PID $! (log: /logs/browser-upload.log)"
