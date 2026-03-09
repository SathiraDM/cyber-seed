#!/usr/bin/env python3
"""
find-video-url.py — Extract the real direct video URL from a faphouse page.

Uses yt-dlp --write-pages to let yt-dlp handle authenticated page fetching
(with Chromium cookie decryption), then parses data-el-formats / data-el-hls-url
from the dumped HTML to get the signed video URL.

Prints the direct video URL to stdout.

Usage:
    python3 /scripts/find-video-url.py <page_url> [quality]
    quality: 2160 | 1080 | 720 | 480 | 360 | 240  (default: best available)
"""
import sys
import re
import json
import subprocess
import tempfile
import shutil
import os
import html as html_mod

BROWSER_PROFILE = "/config/browser/chromium"
PREFERRED_QUALITIES = ["2160", "1080", "720", "480", "360", "240"]


def fetch_page_via_ytdlp(page_url: str, tmpdir: str) -> str | None:
    """Run yt-dlp --write-pages --no-download, return path to the .dump file."""
    cmd = [
        "yt-dlp",
        "--cookies-from-browser", f"chromium:{BROWSER_PROFILE}",
        "--write-pages",
        "--no-download",
        "--no-playlist",
        "--playlist-items", "1",  # only need first entry to get the page dump
        "--quiet",
        "--no-warnings",
        page_url,
    ]
    print(f"[find-video] Fetching page via yt-dlp...", file=sys.stderr)
    try:
        subprocess.run(cmd, cwd=tmpdir, timeout=60, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print("[find-video] yt-dlp --write-pages timed out", file=sys.stderr)
        return None

    # Find the .dump file (yt-dlp names it based on the URL)
    dumps = [f for f in os.listdir(tmpdir) if f.endswith(".dump")]
    print(f"[find-video] Dump files: {dumps}", file=sys.stderr)
    if not dumps:
        return None

    # Return the largest dump file (the main page, not sub-requests)
    best = max(dumps, key=lambda f: os.path.getsize(os.path.join(tmpdir, f)))
    return os.path.join(tmpdir, best)


def extract_url_from_page(html: str, target_quality: str | None = None) -> str | None:
    """Parse data-el-formats or data-el-hls-url from the page HTML."""

    # Strategy 1: data-el-formats — direct signed MP4 URLs per quality
    m = re.search(r'data-el-formats=["\']([^"\']+)["\']', html)
    if m:
        raw = html_mod.unescape(m.group(1))
        try:
            formats = json.loads(raw)
            print(f"[find-video] data-el-formats keys: {list(formats.keys())}", file=sys.stderr)
            qualities = [target_quality] if target_quality else PREFERRED_QUALITIES
            for q in qualities:
                if q in formats:
                    url = formats[q]
                    print(f"[find-video] Selected quality={q}: {url}", file=sys.stderr)
                    return url
            # fallback: highest key
            best_q = max(formats.keys(), key=lambda k: int(k) if k.isdigit() else 0)
            print(f"[find-video] Fallback quality={best_q}: {formats[best_q]}", file=sys.stderr)
            return formats[best_q]
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[find-video] data-el-formats parse error: {e}", file=sys.stderr)

    # Strategy 2: data-el-hls-url — HLS manifest template with _TPL_ placeholder
    m = re.search(r'data-el-hls-url=["\']([^"\']+)["\']', html)
    if m:
        template = html_mod.unescape(m.group(1))
        q = target_quality or "1080"
        if "_TPL_" in template:
            url = template.replace("_TPL_", q)
        else:
            url = template
        print(f"[find-video] data-el-hls-url quality={q}: {url}", file=sys.stderr)
        return url

    # Strategy 3: preload fetch link with m3u8
    for m in re.finditer(r'<link[^>]+as=["\']fetch["\'][^>]+href=["\']([^"\']+\.m3u8[^"\']*)["\']',
                         html, re.IGNORECASE):
        template = html_mod.unescape(m.group(1))
        q = target_quality or "1080"
        url = template.replace("_TPL_", q) if "_TPL_" in template else template
        print(f"[find-video] preload link quality={q}: {url}", file=sys.stderr)
        return url

    return None


def main():
    if len(sys.argv) < 2:
        print("Usage: find-video-url.py <url> [quality]", file=sys.stderr)
        sys.exit(1)

    page_url = sys.argv[1].split("#")[0]
    target_quality = sys.argv[2] if len(sys.argv) > 2 else None

    tmpdir = tempfile.mkdtemp(prefix="find-video-")
    try:
        dump_path = fetch_page_via_ytdlp(page_url, tmpdir)
        if not dump_path:
            print("[find-video] ERROR: yt-dlp did not produce a page dump.", file=sys.stderr)
            sys.exit(1)

        print(f"[find-video] Reading dump: {dump_path} ({os.path.getsize(dump_path)} bytes)", file=sys.stderr)
        with open(dump_path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()

        url = extract_url_from_page(html, target_quality)
        if not url:
            print("[find-video] ERROR: Could not find video URL in page dump.", file=sys.stderr)
            sys.exit(1)

        print(url)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
