#!/usr/bin/env python3
"""
export-cookies.py — Extract Chromium cookies and write Netscape cookies file.

Reads the Chromium SQLite cookies database from the browser container's
mounted config volume, decrypts cookie values (Linux PBKDF2 method),
and writes a Netscape-format cookies.txt for yt-dlp to use.

Usage:
    python3 /scripts/export-cookies.py [domain_filter]

    domain_filter: optional domain to filter (e.g. faphouse.com)
                   defaults to exporting ALL cookies
"""
import os
import sys
import shutil
import sqlite3
import struct
import tempfile
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────
# Browser config is mounted read-only at /config/browser in qbt container.
# jlesage/chromium stores user data under /config/xdg (HOME=/config/xdg).
CHROMIUM_PROFILE_SEARCH_PATHS = [
    "/config/browser/xdg/config/chromium/Default",
    "/config/browser/xdg/.config/chromium/Default",
    "/config/browser/.config/chromium/Default",
    "/config/browser/config/chromium/Default",
]
OUTPUT_FILE = "/config/rclone/faphouse-cookies.txt"

domain_filter = sys.argv[1] if len(sys.argv) > 1 else None
# Strip protocol/path if user passed a full URL
if domain_filter:
    if '://' in domain_filter:
        from urllib.parse import urlparse
        domain_filter = urlparse(domain_filter).hostname or domain_filter
    domain_filter = domain_filter.lstrip('www.').strip('/')


def find_cookies_db():
    for profile in CHROMIUM_PROFILE_SEARCH_PATHS:
        db = os.path.join(profile, "Cookies")
        if os.path.exists(db):
            return db
    # Fallback: search recursively under /config/browser
    for root, dirs, files in os.walk("/config/browser"):
        if "Cookies" in files:
            candidate = os.path.join(root, "Cookies")
            # Quick check it looks like a Chromium cookies DB
            try:
                with open(candidate, "rb") as f:
                    header = f.read(16)
                if b"SQLite" in header:
                    return candidate
            except Exception:
                pass
    return None


def get_linux_chrome_key():
    """Derive the AES-128-CBC key Chromium uses on Linux without a keyring."""
    try:
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.backends import default_backend

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA1(),
            length=16,
            salt=b"saltysalt",
            iterations=1,
            backend=default_backend(),
        )
        return kdf.derive(b"peanuts")
    except ImportError:
        return None


def decrypt_v10(encrypted_value, key):
    """Decrypt a v10-prefixed Chromium cookie value (AES-128-CBC)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend

        ciphertext = encrypted_value[3:]  # strip 'v10' prefix
        iv = b" " * 16  # Chromium uses 16 spaces as IV on Linux
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        # Remove PKCS7 padding
        pad_len = padded[-1]
        return padded[:-pad_len].decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  Warning: decryption error: {e}")
        return ""


def decrypt_cookie_value(encrypted_value, raw_value, key):
    """Decrypt or return raw cookie value depending on format."""
    if not encrypted_value:
        return raw_value or ""
    if isinstance(encrypted_value, bytes):
        if encrypted_value.startswith(b"v10") and key:
            return decrypt_v10(encrypted_value, key)
        elif encrypted_value.startswith(b"v11") and key:
            # v11 on Linux also uses same AES-128-CBC (same as v10)
            return decrypt_v10(encrypted_value, key)
        else:
            # Try raw UTF-8 decode (unencrypted or unknown format)
            try:
                return encrypted_value.decode("utf-8", errors="replace")
            except Exception:
                return ""
    return str(encrypted_value) if encrypted_value else raw_value or ""


def chrome_time_to_unix(chrome_time):
    """Convert Chrome timestamp (microseconds since 1601-01-01) to Unix epoch."""
    if not chrome_time:
        return 0
    # Chrome epoch: Jan 1, 1601. Unix epoch: Jan 1, 1970. Diff = 11644473600s
    unix = int(chrome_time / 1_000_000) - 11644473600
    return max(0, unix)


def main():
    # ── Find cookies DB ───────────────────────────────────────────────
    db_path = find_cookies_db()
    if not db_path:
        print("ERROR: Chromium cookies database not found.")
        print("Searched paths:")
        for p in CHROMIUM_PROFILE_SEARCH_PATHS:
            print(f"  {p}")
        print("")
        print("Make sure:")
        print("  1. The virtual browser has been opened at least once.")
        print("  2. You have logged in to faphouse.com.")
        print("  3. The browser config is mounted at /config/browser in this container.")
        sys.exit(1)

    print(f"Found cookies DB: {db_path}")

    # ── Derive decryption key ─────────────────────────────────────────
    key = get_linux_chrome_key()
    if key:
        print("Decryption key derived (PBKDF2/peanuts).")
    else:
        print("Warning: cryptography module not available — encrypted cookies may be empty.")
        print("Install py3-cryptography for full support.")

    # ── Copy DB (Chromium may have it locked) ─────────────────────────
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(db_path, tmp)

    try:
        conn = sqlite3.connect(tmp)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        if domain_filter:
            cur.execute(
                "SELECT * FROM cookies WHERE host_key LIKE ? OR host_key LIKE ?",
                (f"%{domain_filter}%", f"%.{domain_filter}%"),
            )
        else:
            cur.execute("SELECT * FROM cookies")

        rows = cur.fetchall()
        conn.close()
    finally:
        os.unlink(tmp)

    if not rows:
        msg = f"for '{domain_filter}'" if domain_filter else "in the browser"
        print(f"WARNING: No cookies found {msg}.")
        print("Make sure you have logged in to faphouse.com in the virtual browser first.")
        sys.exit(1)

    print(f"Found {len(rows)} cookies{f' for {domain_filter}' if domain_filter else ''}.")

    # ── Write Netscape cookies file ───────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    with open(OUTPUT_FILE, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(f"# Exported by CyberSeed on {datetime.utcnow().isoformat()}Z\n")
        f.write(f"# Source: {db_path}\n\n")

        exported = 0
        for row in rows:
            host    = row["host_key"] or ""
            flag    = "TRUE" if host.startswith(".") else "FALSE"
            path    = row["path"] or "/"
            secure  = "TRUE" if row["is_secure"] else "FALSE"
            expiry  = chrome_time_to_unix(row["expires_utc"])
            name    = row["name"] or ""
            value   = decrypt_cookie_value(
                row["encrypted_value"],
                row["value"],
                key,
            )
            f.write(f"{host}\t{flag}\t{path}\t{secure}\t{expiry}\t{name}\t{value}\n")
            exported += 1

    print(f"✓ Exported {exported} cookies → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
