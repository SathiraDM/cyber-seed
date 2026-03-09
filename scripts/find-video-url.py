#!/usr/bin/env python3
"""
find-video-url.py — Intercept the real signed video URL via browser CDP.

Connects to the Chromium container (cyber-seed-browser) over Chrome DevTools
Protocol, opens the faphouse video page with your session cookies injected,
waits for the player to fire its network request for the actual signed CDN
video file, and prints the URL to stdout.

Usage:
    python3 /scripts/find-video-url.py <page_url> [quality]
    quality: 1080 | 720 | 480 | 240  (default: 1080)

Requirements:
    - cyber-seed-browser container running with CDP enabled
      (CHROMIUM_EXTRA_PARAMS=--remote-debugging-port=9222 --remote-debugging-address=0.0.0.0)
    - /config/cookies/faphouse-cookies.txt (Netscape format)
"""

import sys
import asyncio
import json
import time
import urllib.request

import websockets  # pip: websockets

CDP_HOST = "cyber-seed-browser"
CDP_PORT = 9222
COOKIES_FILE = "/config/cookies/faphouse-cookies.txt"

VIDEO_CDN_HOSTS = ("xhcdn.com", "flixcdn.com", "ahcdn.com")
PREFERRED_QUALITIES = ["2160", "1080", "720", "480", "360", "240"]


# ── Helpers ────────────────────────────────────────────────────────────────

def parse_netscape_cookies(filepath):
    cookies = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            c = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "httpOnly": False,
                "sameSite": "None",
            }
            if expires and expires != "0":
                try:
                    c["expires"] = int(expires)
                except ValueError:
                    pass
            cookies.append(c)
    return cookies


def is_video_url(url):
    if not any(h in url for h in VIDEO_CDN_HOSTS):
        return False
    path = url.split("?")[0]
    if not (path.endswith(".mp4") or path.endswith(".m3u8")):
        return False
    if "/trailer/" in url or "/preview/" in url:
        return False
    return True


def quality_score(url, preferred):
    for i, q in enumerate(PREFERRED_QUALITIES):
        if ("/" + q + ".") in url or ("/format/" + q) in url or ("/" + q + "p.") in url:
            return (1000 if q == preferred else 100) - i
    return 0


def cdp_http(path):
    return f"http://{CDP_HOST}:{CDP_PORT}{path}"


# ── CDP logic ──────────────────────────────────────────────────────────────

async def find_video_url(page_url, target_quality, timeout=30):
    print(f"[find-video] Opening new tab for: {page_url}", file=sys.stderr)
    try:
        with urllib.request.urlopen(cdp_http(f"/json/new?{page_url}"), timeout=10) as r:
            tab = json.loads(r.read())
    except Exception as e:
        print(f"[find-video] ERROR: Cannot reach browser CDP at {CDP_HOST}:{CDP_PORT} — {e}", file=sys.stderr)
        print(f"[find-video] Check that cyber-seed-browser is running with CHROMIUM_EXTRA_PARAMS set.", file=sys.stderr)
        return None

    ws_url = tab["webSocketDebuggerUrl"]
    tab_id = tab["id"]
    print(f"[find-video] Tab id={tab_id}", file=sys.stderr)

    try:
        return await _cdp_capture(ws_url, page_url, target_quality, timeout)
    finally:
        try:
            urllib.request.urlopen(cdp_http(f"/json/close/{tab_id}"), timeout=5).close()
        except Exception:
            pass


async def _cdp_capture(ws_url, page_url, target_quality, timeout):
    msg_id = 0
    found_urls = []
    page_loaded = False
    play_clicked = False

    # JS selectors to try for the play button
    PLAY_JS = """
    (function() {
        var sels = ['.play-btn','.play-button','[data-el-play]','.vjs-big-play-button',
                    '.fp-play','button[aria-label*="play" i]','.player-play',
                    '.video-play-btn','[class*="PlayButton"]'];
        for (var s of sels) { var el=document.querySelector(s); if(el){el.click();return s;} }
        var v=document.querySelector('video'); if(v){v.play();return 'video.play()';}
        return 'none';
    })()
    """

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024, ping_timeout=None) as ws:

        async def send(method, params=None):
            nonlocal msg_id
            msg_id += 1
            _id = msg_id
            await ws.send(json.dumps({"id": _id, "method": method, "params": params or {}}))
            return _id

        await send("Network.enable", {"maxPostDataSize": 0})
        await send("Page.enable")

        # Inject cookies before navigation so they're sent on the first request
        cookies = parse_netscape_cookies(COOKIES_FILE)
        await send("Network.setCookies", {"cookies": cookies})
        print(f"[find-video] Injected {len(cookies)} cookies", file=sys.stderr)

        await send("Page.navigate", {"url": page_url})

        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(1.5, remaining))
                msg = json.loads(raw)
            except asyncio.TimeoutError:
                if page_loaded and not play_clicked:
                    play_clicked = True
                    print("[find-video] Retrying play click...", file=sys.stderr)
                    await send("Runtime.evaluate", {"expression": PLAY_JS, "returnByValue": True})
                continue

            method = msg.get("method", "")

            if method == "Page.loadEventFired":
                page_loaded = True
                print("[find-video] Page loaded — triggering play...", file=sys.stderr)
                play_clicked = True
                await send("Runtime.evaluate", {
                    "expression": f"setTimeout(function(){{ {PLAY_JS} }}, 1200);",
                    "returnByValue": False,
                })

            elif method == "Network.requestWillBeSent":
                url = msg["params"]["request"]["url"]
                if is_video_url(url):
                    print(f"[find-video] Intercepted: {url[:120]}", file=sys.stderr)
                    found_urls.append(url)
                    # Stop as soon as we have the exact quality we want as an mp4
                    if url.split("?")[0].endswith(".mp4") and quality_score(url, target_quality) >= 1000:
                        break

    if not found_urls:
        return None

    mp4s = [u for u in found_urls if u.split("?")[0].endswith(".mp4")]
    candidates = mp4s if mp4s else found_urls
    candidates.sort(key=lambda u: quality_score(u, target_quality), reverse=True)
    best = candidates[0]
    print(f"[find-video] Selected: {best}", file=sys.stderr)
    return best


# ── Entry point ────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: find-video-url.py <url> [quality]", file=sys.stderr)
        sys.exit(1)

    page_url = sys.argv[1].split("#")[0]
    target_quality = sys.argv[2] if len(sys.argv) > 2 else "1080"

    print(f"[find-video] Target: {page_url}  quality={target_quality}", file=sys.stderr)

    url = asyncio.run(find_video_url(page_url, target_quality))
    if not url:
        print("[find-video] ERROR: No video URL intercepted within timeout.", file=sys.stderr)
        sys.exit(1)

    print(url)


if __name__ == "__main__":
    main()
