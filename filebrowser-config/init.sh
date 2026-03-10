#!/bin/sh
# Init script for FileBrowser — runs before the server starts.
# Ensures: DB created with json auth, admin user exists with correct password.
set -e

DB="/database/filebrowser.db"

if [ ! -f "$DB" ]; then
  echo "[fb-init] Creating database with json auth..."
  filebrowser config init --auth.method=json -d "$DB"
  filebrowser config set --minimumPasswordLength 1 --address 0.0.0.0 --port 80 -d "$DB"
  echo "[fb-init] Adding admin user..."
  filebrowser users add admin "${QBT_WEBUI_PASS}" --perm.admin -d "$DB"
  echo "[fb-init] Done."
else
  echo "[fb-init] Database exists, ensuring json auth + correct address..."
  filebrowser config set --auth.method=json --minimumPasswordLength 1 --address 0.0.0.0 --port 80 -d "$DB"
  filebrowser users update admin -p "${QBT_WEBUI_PASS}" -d "$DB" 2>/dev/null || true
  echo "[fb-init] Done."
fi

exec filebrowser "$@"
