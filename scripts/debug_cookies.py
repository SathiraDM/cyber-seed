#!/usr/bin/env python3
"""Debug script: recover actual IV used in Chromium cookie encryption via known-plaintext attack."""
import sqlite3, shutil, tempfile, os, sys
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.padding import PKCS7

BROWSER_PROFILE = "/config/browser/chromium"
COOKIES_DB = os.path.join(BROWSER_PROFILE, "Default", "Cookies")

tmp = tempfile.mktemp(suffix=".db")
shutil.copy2(COOKIES_DB, tmp)
conn = sqlite3.connect(tmp)
rows = dict(conn.execute(
    'SELECT name, encrypted_value FROM cookies WHERE host_key LIKE "%faphouse%"'
).fetchall())
conn.close()
os.unlink(tmp)

print(f"Found {len(rows)} faphouse cookies: {list(rows.keys())}")

kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt", iterations=1, backend=default_backend())
key = kdf.derive(b"peanuts")

def aes_decrypt(ct, key, iv):
    if len(ct) % 16 != 0:
        ct = ct + b"\x00" * (16 - len(ct) % 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    d = cipher.decryptor()
    raw = d.update(ct) + d.finalize()
    try:
        up = PKCS7(128).unpadder()
        return up.update(raw) + up.finalize()
    except:
        return raw

def decrypt_single_block_ecb(block, key):
    """Decrypt single block with zero IV to get raw AES_decrypt(block)."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(b"\x00"*16), backend=default_backend())
    d = cipher.decryptor()
    raw = d.update(block + block) + d.finalize()
    return raw[:16]

# Step 1: Try static IV (standard Linux Chrome)
print("\n=== Try static IV b' '*16 ===")
for name, enc in rows.items():
    raw = aes_decrypt(enc[3:], key, b" "*16)
    print(f"  {name}: {repr(raw.decode('utf-8', errors='replace')[:50])}")

# Step 2: Known-plaintext attack: locale="en" → recover IV
print("\n=== Known-plaintext attack (locale='en') ===")
locale_enc = rows.get("locale")
if locale_enc:
    c0 = locale_enc[3:19]
    aes_dec_c0 = decrypt_single_block_ecb(c0, key)
    # locale="en" → PKCS7 padded to 16: b"en" + b"\x0e"*14
    p0 = b"en" + b"\x0e"*14
    iv_recovered = bytes(a ^ b for a, b in zip(aes_dec_c0, p0))
    print(f"c0 hex: {c0.hex()}")
    print(f"AES_dec(c0, zero_iv): {aes_dec_c0.hex()}")
    print(f"expected p0: {p0.hex()}")
    print(f"RECOVERED IV: {iv_recovered.hex()} = {repr(iv_recovered)}")

    print("\n=== All cookies with recovered IV ===")
    for name, enc in rows.items():
        raw = aes_decrypt(enc[3:], key, iv_recovered)
        print(f"  {name}: {repr(raw.decode('utf-8', errors='replace')[:60])}")
else:
    print("locale cookie not found, cannot recover IV")

# Step 3: Also check if locale value might be different
print("\n=== Raw cookie DB dump ===")
tmp2 = tempfile.mktemp(suffix=".db")
shutil.copy2(COOKIES_DB, tmp2)
conn2 = sqlite3.connect(tmp2)
all_rows = conn2.execute(
    'SELECT host_key, name, value, encrypted_value FROM cookies WHERE host_key LIKE "%faphouse%"'
).fetchall()
conn2.close()
os.unlink(tmp2)
for host, name, value, enc in all_rows:
    print(f"  {name} (host={host}): value={repr(value)}, enc_len={len(enc)}, enc_prefix={enc[:4]!r}")
