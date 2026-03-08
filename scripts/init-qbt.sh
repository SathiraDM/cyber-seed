#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  init-qbt.sh  (runs at container start via /custom-cont-init.d/)
#
#  1. Writes basic conf settings (LegalNotice, WebUI subnet whitelist)
#  2. Spawns a background process that waits for qBittorrent's API
#     then uses setPreferences to configure AutoRun — this way
#     qBittorrent writes its own keys in its own format.
# ─────────────────────────────────────────────────────────────────────

CONF_DIR="/config/qBittorrent"
CONF_FILE="$CONF_DIR/qBittorrent.conf"
QBT_URL="http://localhost:8080"

# Wait for linuxserver init to create the config directory
for i in $(seq 1 30); do
    [[ -d "$CONF_DIR" ]] && break
    echo "[init-qbt] Waiting for config dir... ($i)"
    sleep 1
done

if [[ ! -d "$CONF_DIR" ]]; then
    echo "[init-qbt] WARNING: Config dir not found after 30s, skipping."
    exit 0
fi

# Create config file if it doesn't exist yet (first boot)
if [[ ! -f "$CONF_FILE" ]]; then
    echo "[init-qbt] Creating default qBittorrent.conf ..."
    cat > "$CONF_FILE" <<'EOF'
[LegalNotice]
Accepted=true

[Preferences]
WebUI\Address=*
WebUI\AuthSubnetWhitelistEnabled=true
WebUI\AuthSubnetWhitelist=127.0.0.1/32
EOF
fi

# ── Pre-seed subnet whitelist so API calls from localhost work ────────
# (These are safe to write before qBittorrent starts — it reads them on boot)
inject_setting() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "$CONF_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$CONF_FILE"
    else
        # Find [Preferences] section and insert after it
        if grep -q "^\[Preferences\]" "$CONF_FILE"; then
            sed -i "/^\[Preferences\]/a ${key}=${value}" "$CONF_FILE"
        else
            printf '\n[Preferences]\n%s=%s\n' "$key" "$value" >> "$CONF_FILE"
        fi
    fi
}

inject_setting 'WebUI\Address'                    '*'
inject_setting 'WebUI\AuthSubnetWhitelistEnabled' 'true'
inject_setting 'WebUI\AuthSubnetWhitelist'        '127.0.0.1/32'

# Accept legal notice
if ! grep -q 'Accepted=true' "$CONF_FILE"; then
    printf '\n[LegalNotice]\nAccepted=true\n' >> "$CONF_FILE"
fi

chmod 600 "$CONF_FILE"
echo "[init-qbt] Base config written. Spawning API configurator in background..."

# ── Background: wait for API, then set AutoRun via setPreferences ─────
# Uses setsid to create a new process session so s6-overlay doesn't
# kill this background job when the init script's process group is reaped.
LOG=/config/init-qbt-api.log
setsid bash -c '
    QBT_URL="http://localhost:8080"
    AUTORUN_CMD="/bin/bash /scripts/on-complete.sh \"%N\" \"%F\" \"%D\" \"%I\""
    LOG=/config/init-qbt-api.log

    echo "[init-qbt-api] Waiting for qBittorrent API at ${QBT_URL} ..."

    for i in $(seq 1 60); do
        if curl -sf --max-time 3 "${QBT_URL}/api/v2/app/version" >/dev/null 2>&1; then
            echo "[init-qbt-api] API is up (attempt $i)."
            break
        fi
        sleep 3
    done

    # Set AutoRun preferences via API (127.0.0.1 is whitelisted — no auth needed)
    RESPONSE=$(curl -sf --max-time 10 \
        -X POST "${QBT_URL}/api/v2/app/setPreferences" \
        --data-urlencode "json={\"autorun_on_torrent_finish_enabled\":true,\"autorun_on_torrent_finish_program\":\"${AUTORUN_CMD}\"}" \
        2>&1)
    EXIT=$?

    if [[ $EXIT -eq 0 ]]; then
        echo "[init-qbt-api] AutoRun configured via API: ${AUTORUN_CMD}"
    else
        echo "[init-qbt-api] ERROR: setPreferences failed (exit $EXIT): $RESPONSE"
    fi

    # Verify it took effect
    PREFS=$(curl -sf --max-time 5 "${QBT_URL}/api/v2/app/preferences" 2>/dev/null)
    echo "[init-qbt-api] Verify: $(echo "$PREFS" | grep -o '"autorun_on_torrent_finish_program":"[^"]*"')"
' >>"$LOG" 2>&1 &

echo "[init-qbt] init-qbt.sh complete. API configurator detached (setsid PID $!)."
