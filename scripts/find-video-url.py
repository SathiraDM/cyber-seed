#!/usr/bin/env python3
"""
find-video-url.py — Extract the real video URL from a faphouse page using browser cookies.
Prints the direct video URL (m3u8 or mp4) to stdout.

Parsing strategy (in priority order):
  1. data-el-formats  — HTML-encoded JSON map of quality→signed MP4 URL (e.g. {"1080": "https://..."})
  2. data-el-hls-url  — HLS manifest template URL with _TPL_ placeholder
  3. <link rel="preload" as="fetch"> m3u8 — same _TPL_ pattern in <head>
  4. Generic fallback  — scan for m3u8/mp4 URLs anywhere in the page

Usage:
    python3 /scripts/find-video-url.py <page_url>
"""
import sys
import re
import json
import sqlite3
import shutil
import tempfile
import os
import html as html_mod
import urllib.request
import urllib.parse

BROWSER_COOKIES_DB = "/config/browser/chromium/Default/Cookies"
PREFERRED_QUALITIES = ["2160", "1080", "720", "480", "360", "240"]


def get_cookies(domain_filter):
    """Read plaintext cookie values from Chromium SQLite DB for a domain."""
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(BROWSER_COOKIES_DB, tmp)
    try:
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, value FROM cookies WHERE host_key LIKE ?",
            (f"%{domain_filter}%",)
        ).fetchall()
        conn.close()
    finally:
        os.unlink(tmp)
    return {name: value for name, value in rows if value}


def build_cookie_header(cookies):
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def fetch_page(url, cookie_header):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Cookie": cookie_header,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://faphouse.com/",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Strategy 1: data-el-formats="{...}" — JSON map of quality → direct MP4 URL
# ---------------------------------------------------------------------------
def try_data_el_formats(html):
    m = re.search(r'data-el-formats=["\']([^"\']+)["\']', html)
    if not m:
        return None
    raw = html_mod.unescape(m.group(1))
    try:
        formats = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[find-video] data-el-formats JSON parse error: {e}", file=sys.stderr)
        return None
    for q in PREFERRED_QUALITIES:
        if q in formats:
            url = formats[q]
            print(f"[find-video] Strategy 1 (data-el-formats): quality={q} url={url}", file=sys.stderr)
            return url
    # fallback: highest available key
    for q, url in sorted(formats.items(), key=lambda kv: kv[0], reverse=True):
        print(f"[find-video] Strategy 1 (data-el-formats): quality={q} url={url}", file=sys.stderr)
        return url
    return None


# ---------------------------------------------------------------------------
# Strategy 2: data-el-hls-url=".../_TPL_.mp4.m3u8" — replace _TPL_ with quality
# ---------------------------------------------------------------------------
def try_data_el_hls_url(html):
    m = re.search(r'data-el-hls-url=["\']([^"\']+)["\']', html)
    if not m:
        return None
    template = html_mod.unescape(m.group(1))
    if "_TPL_" not in template:
        print(f"[find-video] Strategy 2 (data-el-hls-url): no _TPL_ in template, using as-is", file=sys.stderr)
        return template
    for q in PREFERRED_QUALITIES:
        url = template.replace("_TPL_", q)
        print(f"[find-video] Strategy 2 (data-el-hls-url): quality={q} url={url}", file=sys.stderr)
        return url
    return None


# ---------------------------------------------------------------------------
# Strategy 3: <link rel="preload" as="fetch"> m3u8 in <head>
# ---------------------------------------------------------------------------
def try_preload_link(html):
    for m in re.finditer(
        r'<link[^>]+as=["\']fetch["\'][^>]+href=["\']([^"\']+\.m3u8[^"\']*)["\']',
        html, re.IGNORECASE
    ):
        template = html_mod.unescape(m.group(1))
        if "_TPL_" in template:
            for q in PREFERRED_QUALITIES:
                url = template.replace("_TPL_", q)
                print(f"[find-video] Strategy 3 (preload link): quality={q} url={url}", file=sys.stderr)
                return url
        else:
            print(f"[find-video] Strategy 3 (preload link): url={template}", file=sys.stderr)
            return template
    return None


# ---------------------------------------------------------------------------
# Strategy 4: Generic scan for signed video URLs anywhere on the page
# ---------------------------------------------------------------------------
def try_generic(html):
    candidates = []

    # JSON key→value with video URL
    for m in re.finditer(
        r'"(?:src|file|url|videoUrl|video_url|hls|stream|manifest)"\s*:\s*"(https?://[^"]+\.(?:m3u8|mp4)[^"]*)"',
        html, re.IGNORECASE
    ):
        candidates.append(m.group(1))

    # <source src="...">
    for m in re.finditer(r'<source[^>]+src=["\']([^"\']+\.(?:m3u8|mp4)[^"\']*)["\']', html, re.IGNORECASE):
        candidates.append(m.group(1))

    # Bare m3u8 URLs
    for m in re.finditer(r'(https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*)', html):
        candidates.append(m.group(1))

    # Deduplicate, prefer m3u8 over mp4
    seen = set()
    unique = []
    for c in candidates:
        c = c.replace("\\u002F", "/").replace("\\/", "/")
        if c not in seen:
            seen.add(c)
            unique.append(c)
    unique.sort(key=lambda u: (0 if ".m3u8" in u else 1, -len(u)))

    if unique:
        print(f"[find-video] Strategy 4 (generic): found {len(unique)} candidate(s), best: {unique[0]}", file=sys.stderr)
        return unique[0]
    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: find-video-url.py <url>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1].split("#")[0]  # strip fragment
    domain = urllib.parse.urlparse(url).netloc.lstrip("www.")

    print(f"[find-video] Fetching: {url}", file=sys.stderr)

    cookies = get_cookies(domain)
    print(f"[find-video] Loaded {len(cookies)} cookies for {domain}", file=sys.stderr)

    cookie_header = build_cookie_header(cookies)
    html = fetch_page(url, cookie_header)
    print(f"[find-video] Page size: {len(html)} chars", file=sys.stderr)

    result = (
        try_data_el_formats(html) or
        try_data_el_hls_url(html) or
        try_preload_link(html) or
        try_generic(html)
    )

    if not result:
        print("[find-video] ERROR: No video URL found on page.", file=sys.stderr)
        sys.exit(1)

    # Print the final URL to stdout for the shell script to capture
    print(result)


if __name__ == "__main__":
    main()
