#!/bin/bash
# ─────────────────────────────────────────────────────────────────────
#  retry-upload.sh  —  Manually upload a specific folder/file to OneDrive
#
#  Use this if a torrent's auto-upload failed and you want to retry,
#  or to manually upload any path to your OneDrive.
#
#  Usage:
#    ./retry-upload.sh "/path/to/local/folder"  "Remote Folder Name"
#    ./retry-upload.sh "./downloads/completed/MyTorrent"
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'

# Load .env
[[ -f ".env" ]] || { echo -e "${RED}[✗]${NC} .env not found. Run ./setup.sh first."; exit 1; }
export $(grep -v '^#' .env | xargs)

LOCAL_PATH="${1:-}"
REMOTE_NAME="${2:-$(basename "${1:-}")}"

if [[ -z "$LOCAL_PATH" ]]; then
    echo "Usage: $0 <local-path> [remote-folder-name]"
    echo ""
    echo "Examples:"
    echo "  $0 ./downloads/completed/MyTorrent"
    echo "  $0 ./downloads/completed/MyTorrent  'My Upload Folder'"
    exit 1
fi

[[ -e "$LOCAL_PATH" ]] || { echo -e "${RED}[✗]${NC} Path not found: $LOCAL_PATH"; exit 1; }
[[ -f "config/rclone/rclone.conf" ]] || { echo -e "${RED}[✗]${NC} rclone.conf not found. Run ./setup.sh first."; exit 1; }

DEST="${ONEDRIVE_REMOTE}:${ONEDRIVE_PATH}/${REMOTE_NAME}"

echo -e "${CYAN}[retry-upload]${NC} Uploading:"
echo "  Source : $LOCAL_PATH"
echo "  Dest   : $DEST"
echo ""

docker run --rm \
    -v "$(pwd)/config/rclone:/config/rclone" \
    -v "$(realpath "$LOCAL_PATH"):/upload:ro" \
    -e RCLONE_CONFIG=/config/rclone/rclone.conf \
    rclone/rclone copy /upload "$DEST" \
        --transfers=4 \
        --checkers=8 \
        --retries=3 \
        --stats=5s \
        --progress

echo ""
echo -e "${GREEN}[✓] Upload complete: $DEST${NC}"

read -rp "Delete local files? [y/N]: " DEL
if [[ "${DEL,,}" == "y" ]]; then
    rm -rf "$LOCAL_PATH"
    echo -e "${YELLOW}[✓] Local files deleted: $LOCAL_PATH${NC}"
fi
