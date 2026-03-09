#!/usr/bin/env python3
"""
find-video-url.py — Extract the real signed video URL from faphouse.com.

Fetches the video page with session cookies, parses the data-el-formats
attribute from the HTML, and returns the direct CDN URL for the requested
quality.  No browser, no CDP, no Playwright required.

Usage:
    python3 /scripts/find-video-url.py <page_url> [quality]
    quality: 1080 | 720 | 480 | 240  (default: 1080)

Requirements:
    - /config/cookies/faphouse-cookies.txt  (Netscape format)
"""

import sys
import json
import re
import urllib.request
import urllib.error
import html
import http.cookiejar

COOKIES_FILE = "/config/cookies/faphouse-cookies.txt"
PREFERRED_QUALITIES = ["2160", "1080", "720", "480", "360", "240"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://faphouse.com/",
}


def load_cookies(filepath):
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(filepath, ignore_discard=True, ignore_expires=True)
    except FileNotFoundError:
        print(f"[find-video] ERROR: Cookies file not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    return jar


def fetch_page(url, jar):
    handler = urllib.request.HTTPCookieProcessor(jar)
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with opener.open(req, timeout=30) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"[find-video] ERROR: HTTP {e.code} fetching page: {url}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[find-video] ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def parse_formats(page_html):
    """
    Extract data-el-formats JSON from the player element.
    The attribute value is HTML-entity encoded JSON, e.g.:
      data-el-formats="{&quot;240&quot;:&quot;https://...&quot;}"
    Returns a dict like {"240": "https://...", "480": "...", "1080": "..."}
    """
    m = re.search(r'data-el-formats=["\']([^"\']+)["\']', page_html)
    if not m:
        # Unquoted variant
        m = re.search(r'data-el-formats=(\{[^>\s]+\})', page_html)
        if not m:
            return None
    decoded = html.unescape(m.group(1))
    try:
        return json.loads(decoded)
    except json.JSONDecodeError as e:
        print(f"[find-video] ERROR: Failed to parse formats JSON: {e}", file=sys.stderr)
        print(f"[find-video] Raw value: {decoded[:300]}", file=sys.stderr)
        return None


def pick_quality(formats, preferred):
    if preferred in formats:
        return formats[preferred], preferred
    pref_idx = next((i for i, q in enumerate(PREFERRED_QUALITIES) if q == preferred), 0)
    for q in PREFERRED_QUALITIES[pref_idx:]:
        if q in formats:
            return formats[q], q
    best_q = sorted(formats.keys(), key=lambda x: int(x) if x.isdigit() else 0, reverse=True)[0]
    return formats[best_q], best_q


def main():
    if len(sys.argv) < 2:
        print("Usage: find-video-url.py <page_url> [quality]", file=sys.stderr)
        sys.exit(1)

    page_url = sys.argv[1].split("#")[0]
    quality = sys.argv[2] if len(sys.argv) > 2 else "1080"

    print(f"[find-video] Page: {page_url}", file=sys.stderr)
    print(f"[find-video] Requested quality: {quality}p", file=sys.stderr)

    jar = load_cookies(COOKIES_FILE)
    print(f"[find-video] Loaded {len(list(jar))} cookies", file=sys.stderr)

    print(f"[find-video] Fetching page...", file=sys.stderr)
    page_html = fetch_page(page_url, jar)

    formats = parse_formats(page_html)
    if not formats:
        print("[find-video] ERROR: Could not find data-el-formats in page HTML.", file=sys.stderr)
        print("[find-video] Are the cookies valid and not expired?", file=sys.stderr)
        sys.exit(1)

    available = sorted(formats.keys(), key=lambda x: int(x) if x.isdigit() else 0, reverse=True)
    print(f"[find-video] Available qualities: {', '.join(available)}p", file=sys.stderr)

    video_url, chosen = pick_quality(formats, quality)
    if chosen != quality:
        print(f"[find-video] {quality}p not available, using {chosen}p", file=sys.stderr)

    print(f"[find-video] URL ({chosen}p): {video_url}", file=sys.stderr)
    print(video_url)


if __name__ == "__main__":
    main()
