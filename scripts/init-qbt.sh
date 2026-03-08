#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  init-qbt.sh  (runs at container start via /custom-cont-init.d/)
#
#  Injects the AutoRun "on torrent finish" command into qBittorrent.conf
#  if it is not already configured. Safe to re-run.
# ─────────────────────────────────────────────────────────────────────

CONF_DIR="/config/qBittorrent"
CONF_FILE="$CONF_DIR/qBittorrent.conf"
SCRIPT="/scripts/on-complete.sh"
AUTORUN_CMD="/bin/bash $SCRIPT \"%N\" \"%F\" \"%D\""

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

# Create config file if it doesn't exist yet
if [[ ! -f "$CONF_FILE" ]]; then
    echo "[init-qbt] Creating default qBittorrent.conf ..."
    cat > "$CONF_FILE" <<'EOF'
[LegalNotice]
Accepted=true
EOF
fi

# ── Inject AutoRun settings (idempotent) ─────────────────────────────
inject_setting() {
    local section="$1"
    local key="$2"
    local value="$3"

    # Check if section exists
    if ! grep -q "^\[$section\]" "$CONF_FILE"; then
        echo "" >> "$CONF_FILE"
        echo "[$section]" >> "$CONF_FILE"
    fi

    # Check if key already exists in section; if so, update it
    if grep -q "^$key=" "$CONF_FILE"; then
        sed -i "s|^$key=.*|$key=$value|" "$CONF_FILE"
        echo "[init-qbt] Updated: [$section] $key"
    else
        # Insert after section header
        sed -i "/^\[$section\]/a $key=$value" "$CONF_FILE"
        echo "[init-qbt] Set: [$section] $key = $value"
    fi
}

# Write AutoRun settings directly (plain regex, no configparser reformatting)
CONF_FILE="$CONF_FILE" python3 - <<'PYEOF'
import re, os

conf_file = os.environ['CONF_FILE']
cmd = r'/bin/bash /scripts/on-complete.sh "%N" "%F" "%D" "%I"'

with open(conf_file, 'r') as f:
    content = f.read()

def set_key(text, section, key, value):
    """Set key=value in [section], case-insensitive key match, preserving exact key name."""
    # Replace existing key (case-insensitive) anywhere in file
    pattern = re.compile(r'^' + re.escape(key) + r'\s*=.*$', re.MULTILINE | re.IGNORECASE)
    if pattern.search(text):
        return pattern.sub(key + '=' + value, text)
    # Insert after [Section] header (case-insensitive section match)
    sec_pattern = re.compile(r'(\[' + re.escape(section) + r'\])', re.IGNORECASE)
    if sec_pattern.search(text):
        return sec_pattern.sub(r'\1\n' + key + '=' + value, text)
    # Section doesn't exist — append it
    return text.rstrip('\n') + '\n\n[' + section + ']\n' + key + '=' + value + '\n'

content = set_key(content, 'AutoRun', 'enabled', 'true')
content = set_key(content, 'AutoRun', 'Program', cmd)

with open(conf_file, 'w') as f:
    f.write(content)

print('[init-qbt] AutoRun written: ' + cmd)
PYEOF

# ── Download paths ────────────────────────────────────────────────────
inject_setting "Preferences" "Downloads\\SavePath"         "/downloads/completed/"
inject_setting "Preferences" "Downloads\\TempPath"         "/downloads/incomplete/"
inject_setting "Preferences" "Downloads\\TempPathEnabled"  "true"

# ── Web UI: accept connections from any IP ────────────────────────────
inject_setting "Preferences" "WebUI\\Address" "*"
# Bypass auth for localhost so on-complete.sh can call the API from inside the container
inject_setting "Preferences" "WebUI\\AuthSubnetWhitelistEnabled" "true"
inject_setting "Preferences" "WebUI\\AuthSubnetWhitelist" "127.0.0.1/32"

# ── Web UI: set username + hashed password from env ───────────────────
if [[ -n "${QBT_WEBUI_PASS:-}" ]]; then
    echo "[init-qbt] Setting Web UI password ..."

    # qBittorrent 4.4+ uses PBKDF2-SHA1: @ByteArray(b64_salt:b64_hash)
    # Pass password via env var to avoid shell escaping issues with special chars
    HASHED=$(QBT_WEBUI_PASS="${QBT_WEBUI_PASS}" python3 - <<'PYEOF'
import hashlib, os, base64
password = os.environ['QBT_WEBUI_PASS'].encode('utf-8')
salt = os.urandom(16)
dk = hashlib.pbkdf2_hmac('sha1', password, salt, 100000, dklen=32)
print('@ByteArray(' + base64.b64encode(salt).decode() + ':' + base64.b64encode(dk).decode() + ')')
PYEOF
)
    inject_setting "Preferences" "WebUI\\Username" "admin"
    inject_setting "Preferences" "WebUI\\Password_PBKDF2" "${HASHED}"
    echo "[init-qbt] Web UI credentials configured (admin / [hidden])."
else
    echo "[init-qbt] QBT_WEBUI_PASS not set — keeping default credentials."
fi

echo "[init-qbt] qBittorrent config ready."
chmod 600 "$CONF_FILE"
