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

# ── Write the API configurator to a file (avoids quoting issues) ──────
cat > /tmp/configure-autorun.sh << 'APIEOF'
#!/bin/bash
QBT_URL="http://localhost:8080"
LOG=/config/init-qbt-api.log
CMD='/bin/bash /scripts/on-complete.sh "%N" "%F" "%D" "%I"'
PASS="${QBT_WEBUI_PASS:-adminadmin}"

echo "[init-qbt-api] Started. Waiting for qBittorrent API..." >> "$LOG"

for i in $(seq 1 60); do
    STATUS=$(curl -so /dev/null -w "%{http_code}" --max-time 3 "${QBT_URL}/api/v2/app/version" 2>/dev/null)
    if [[ "$STATUS" == "200" || "$STATUS" == "403" ]]; then
        echo "[init-qbt-api] API up (HTTP $STATUS) after ${i} attempts." >> "$LOG"
        break
    fi
    sleep 3
done

# Login to get a session cookie
COOKIE_JAR=/tmp/qbt-cookies.txt
LOGIN=$(curl -sf --max-time 10 \
    -c "$COOKIE_JAR" \
    --data "username=admin&password=${PASS}" \
    "${QBT_URL}/api/v2/auth/login" 2>&1)
echo "[init-qbt-api] Login response: $LOGIN" >> "$LOG"

# Set AutoRun preferences
    JSON="{\"autorun_enabled\":true,\"autorun_program\":\"${CMD}\"}"
RESPONSE=$(curl -sf --max-time 10 \
    -b "$COOKIE_JAR" \
    -X POST "${QBT_URL}/api/v2/app/setPreferences" \
    --data-urlencode "json=${JSON}" 2>&1)
EXIT=$?

if [[ $EXIT -eq 0 ]]; then
    echo "[init-qbt-api] SUCCESS: AutoRun set to: $CMD" >> "$LOG"
else
    echo "[init-qbt-api] ERROR (exit $EXIT): $RESPONSE" >> "$LOG"
fi

# Verify
PROG=$(curl -sf --max-time 5 -b "$COOKIE_JAR" "${QBT_URL}/api/v2/app/preferences" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('autorun_program','NOT SET'))" 2>/dev/null || echo "parse error")
echo "[init-qbt-api] Verified program: $PROG" >> "$LOG"
rm -f "$COOKIE_JAR"
APIEOF

chmod +x /tmp/configure-autorun.sh

# Export password so setsid child inherits it
export QBT_WEBUI_PASS

# setsid detaches from s6-overlay's process group so it isn't killed on exit
setsid /tmp/configure-autorun.sh &

echo "[init-qbt] init-qbt.sh complete. API configurator detached."
