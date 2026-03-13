#!/usr/bin/env python3
"""
CyberSeed Web UI v2 — SQLite-backed, unified job model, real-time progress via SSE.
"""

import docker
import functools
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, stream_with_context, url_for)
from flask_socketio import SocketIO

import db

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

JOBS_DIR       = Path("/logs/jobs")
DOWNLOADS_ROOT = Path(os.environ.get("DOWNLOADS_ROOT", "/downloads"))
QBT_CONTAINER  = os.environ.get("QBT_CONTAINER", "cyber-seed-qbt")
WEBUI_PASS     = os.environ.get("QBT_WEBUI_PASS", "")
BROWSER_PORT   = os.environ.get("BROWSER_PORT", "3456")
app.secret_key = hashlib.sha256(f"cyber-seed:{WEBUI_PASS}".encode()).hexdigest()
JOBS_DIR.mkdir(parents=True, exist_ok=True)

db.init_db()

# ── Orphan recovery ───────────────────────────────────────────────────
# If the webui restarted while a download was in progress, jobs can get
# stuck in "downloading/running" forever because the monitoring thread died.
# On startup we re-attach: for each stuck job, poll the qbt container
# until yt-dlp for that filename is gone, then resolve to done/failed.

def _recover_orphan(job):
    """Background thread: waits for an orphaned exec to finish, then updates DB."""
    job_id  = job["id"]
    name    = job.get("name", "")
    source  = job.get("source", "")
    log_path = JOBS_DIR / f"{job_id}.log"

    def _log(msg):
        with open(log_path, "a") as f:
            f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] [recovery] {msg}\n")

    _log(f"Recovering orphaned job (webui restarted mid-download): {name}")

    try:
        container = docker_client.containers.get(QBT_CONTAINER)
    except Exception:
        db.update_job(job_id, status="failed", ended_at=datetime.utcnow().isoformat(),
                      error="Container not found during recovery")
        return

    # Poll until yt-dlp for this job is no longer running (or 2h timeout)
    deadline = time.time() + 7200
    safe_name_fragment = name[:30] if name else ""
    while time.time() < deadline:
        try:
            result = container.exec_run("ps aux")
            procs = result.output.decode("utf-8", errors="replace")
            still_running = "yt-dlp" in procs and (not safe_name_fragment or safe_name_fragment in procs)
            if not still_running:
                break
        except Exception:
            break
        time.sleep(10)

    # Determine outcome by checking if the output file exists
    # Use list form to avoid shell glob-expansion of brackets in filenames
    file_found = False
    try:
        if source == "fh":
            # New layout: MP4 is in #moved/<stem>/
            _stem = name[:-4] if name.endswith(".mp4") else name
            result = container.exec_run(["test", "-f", f"/downloads/faphouse/#moved/{_stem}/{name}"])
            file_found = result.exit_code == 0
        elif name:
            result = container.exec_run(["test", "-d", f"/downloads/{name}"])
            file_found = result.exit_code == 0
    except Exception:
        pass

    if file_found and source == "fh":
        _log("Recovery: download file found — resuming contact sheet + upload.")
        safe_name = name
        stem = safe_name[:-4]
        vid_dir = f"/downloads/faphouse/#moved/{stem}"
        # Find thumbnail
        thumb_name = None
        for ext in ("jpg", "jpeg", "png", "webp"):
            r = container.exec_run(["test", "-f", f"{vid_dir}/{stem}.{ext}"])
            if r.exit_code == 0:
                thumb_name = f"{stem}.{ext}"
                break
        # Generate contact sheet if not already present
        sheet_name = None
        sheet_path = f"{vid_dir}/{stem}.preview.jpg"
        r = container.exec_run(["test", "-f", sheet_path])
        if r.exit_code != 0:
            if _generate_contact_sheet(container, f"{vid_dir}/{safe_name}",
                                       sheet_path, _log):
                sheet_name = f"{stem}.preview.jpg"
        else:
            sheet_name = f"{stem}.preview.jpg"
        # Run uploads
        try:
            c_info = docker_client.api.inspect_container(container.id)
            env_dict = dict(e.split("=", 1) for e in c_info["Config"]["Env"] if "=" in e)
            rclone_conf = env_dict.get("RCLONE_CONFIG", "/config/rclone/rclone.conf")

            od_remote = env_dict.get("ONEDRIVE_REMOTE", "onedrive")
            od_path   = env_dict.get("WEBDL_PATH", "/WebDownloads")
            od_dest   = f"{od_remote}:{od_path}/faphouse/{stem}"
            _log(f"Recovery: uploading → OneDrive {od_dest}/")
            db.update_job(job_id, status="uploading", upload_pct=0, upload_status="OneDrive")
            od_cmd = ["rclone", "copy", vid_dir + "/", od_dest,
                      "--config", rclone_conf, "--retries", "10",
                      "--low-level-retries", "20", "--no-check-dest",
                      "--use-json-log", "--stats=2s", "-v"]
            od_exec = docker_client.api.exec_create(container.id, od_cmd)["Id"]
            for chunk in docker_client.api.exec_start(od_exec, stream=True):
                if chunk:
                    with open(log_path, "a") as f:
                        f.write(chunk.decode("utf-8", errors="replace"))
            od_exit = docker_client.api.exec_inspect(od_exec)["ExitCode"]
            _log("Recovery: OneDrive upload complete." if od_exit == 0 else f"Recovery: OneDrive upload failed (exit {od_exit})")

            gcs_remote = env_dict.get("GCS_REMOTE", "gcs")
            gcs_bucket = env_dict.get("GCS_BUCKET", "cyberseed-bucket-01")
            gcs_dest   = f"{gcs_remote}:{gcs_bucket}/faphouse/{stem}"
            _log(f"Recovery: uploading → GCS {gcs_dest}/")
            db.update_job(job_id, upload_pct=0, upload_status="GCS")
            gcs_cmd = ["rclone", "copy", vid_dir + "/", gcs_dest,
                       "--gcs-bucket-policy-only",
                       "--config", rclone_conf, "--retries", "10",
                       "--low-level-retries", "20", "--no-check-dest",
                       "--use-json-log", "--stats=2s", "-v"]
            gcs_exec = docker_client.api.exec_create(container.id, gcs_cmd)["Id"]
            for chunk in docker_client.api.exec_start(gcs_exec, stream=True):
                if chunk:
                    with open(log_path, "a") as f:
                        f.write(chunk.decode("utf-8", errors="replace"))
            gcs_exit = docker_client.api.exec_inspect(gcs_exec)["ExitCode"]
            if gcs_exit == 0:
                _log("Recovery: GCS upload complete.")
                container.exec_run(["rm", "-f", f"{vid_dir}/{safe_name}"])
                _log(f"Recovery: MP4 deleted from #moved/{stem}/")
            else:
                _log(f"Recovery: GCS upload failed (exit {gcs_exit})")
        except Exception as ue:
            _log(f"Recovery: upload error: {ue}")
        db.update_job(job_id, status="done", download_pct=100.0,
                      ended_at=datetime.utcnow().isoformat())
        _log("Recovery: job completed.")
    elif file_found:
        db.update_job(job_id, status="done", download_pct=100.0,
                      ended_at=datetime.utcnow().isoformat())
        _log("Recovery: download completed successfully.")
    else:
        db.update_job(job_id, status="failed",
                      ended_at=datetime.utcnow().isoformat(),
                      error="Process not found after webui restart — may have completed; check /downloads")
        _log("Recovery: could not confirm completion; marked failed.")


def _start_orphan_recovery():
    """Called once at startup to recover jobs stuck in active states."""
    if not docker_client:
        return
    stuck = db.list_jobs(source="", status="downloading", page=1, per_page=100, search="")[0]
    stuck += db.list_jobs(source="", status="running", page=1, per_page=100, search="")[0]
    stuck += db.list_jobs(source="", status="processing", page=1, per_page=100, search="")[0]
    for job in stuck:
        threading.Thread(target=_recover_orphan, args=(job,), daemon=True).start()
        print(f"[webui] Recovering orphaned job {job['id']} ({job.get('name','?')})")


# Pushes a 'refresh' event to all connected clients.
# Runs fast (1s) when active jobs exist, slow (8s) when idle.

def _jobs_push_thread():
    ACTIVE_STATUSES = {"running", "downloading", "uploading", "processing", "queued", "pending"}
    while True:
        try:
            jobs = db.list_jobs(source="", status="", page=1, per_page=200, search="")[0]
            has_active = any(j["status"] in ACTIVE_STATUSES for j in jobs)
            socketio.emit("refresh", {"active": has_active})
            time.sleep(1 if has_active else 8)
        except Exception:
            time.sleep(8)

_push_thread = threading.Thread(target=_jobs_push_thread, daemon=True)
_push_thread.start()

# ── CORS (Chrome extension) ──────────────────────────────────────────

@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "")
    if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://"):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Key"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response

@app.before_request
def handle_options():
    if request.method == "OPTIONS":
        origin = request.headers.get("Origin", "")
        if origin.startswith("chrome-extension://") or origin.startswith("moz-extension://"):
            resp = Response("", 204)
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Api-Key"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            return resp

# ── Auth ──────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-Api-Key", "")
        if api_key and api_key == WEBUI_PASS:
            return f(*args, **kwargs)
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ── Docker ────────────────────────────────────────────────────────────

try:
    docker_client = docker.from_env()
except Exception as e:
    print(f"[webui] Docker client error: {e}")
    docker_client = None

# Kick off recovery for any jobs orphaned by a previous webui restart.
threading.Thread(target=_start_orphan_recovery, daemon=True).start()

# ── Progress parsing ──────────────────────────────────────────────────

def parse_ytdlp_progress(line):
    # HLS format: [download]   0.4% of ~ 843.97MiB at    3.03MiB/s ETA 04:21 (frag 2/437)
    # Note: there is a space between '~' and the size, hence ~?\s* .
    m = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*\w+)\s+at\s+([\d.]+\s*\w+/s)\s+ETA\s+(\S+)(?:.*\(frag\s+(\d+)/(\d+)\))?', line)
    if m:
        frag_num, frag_tot = m.group(5), m.group(6)
        if frag_num and frag_tot and int(frag_tot) > 0:
            pct = round(int(frag_num) / int(frag_tot) * 100, 1)
            # Show stable frag counter instead of the wildly fluctuating ~ estimate
            size_label = f"frag {frag_num}/{frag_tot}"
        else:
            pct = float(m.group(1))
            size_label = m.group(2).strip()
        eta = m.group(4).strip()
        return {"download_pct": pct, "file_size": size_label,
                "speed": m.group(3).strip(), "eta": "" if eta == "Unknown" else eta}
    m2 = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+~?\s*([\d.]+\s*\w+)\s+at\s+([\d.]+\s*\w+/s)', line)
    if m2:
        return {"download_pct": float(m2.group(1)), "file_size": m2.group(2).strip(),
                "speed": m2.group(3).strip()}
    # Final summary line: [download] 100% of  699.89MiB in 00:03:30 at 3.33MiB/s
    # Per-fragment completions look identical but with tiny durations like 00:00:01.
    # The real end-of-file summary always has elapsed >= 1 minute (mm:ss or hh:mm:ss).
    m3 = re.search(r'\[download\] 100% of\s+([\d.]+\s*\w+)\s+in\s+(\d+:\d+:\d+|\d+:\d+)\s+at\s+([\d.]+\s*\w+/s)', line)
    if m3:
        duration = m3.group(2)
        parts = duration.split(':')
        total_secs = sum(int(p) * (60 ** i) for i, p in enumerate(reversed(parts)))
        if total_secs >= 10:  # per-fragment lines are always < 10s
            return {"download_pct": 100.0, "file_size": m3.group(1).strip(), "speed": m3.group(3).strip(), "eta": ""}
    return None


def parse_ffmpeg_progress(line, total_secs=None):
    # ffmpeg stderr stats: size=  175616KiB time=00:09:56.65 bitrate=2411.2kbits/s speed=45.9x
    m = re.search(r'size=\s*(\d+)Ki?B\s+time=(\d+):(\d+):([\d.]+)\s+bitrate=\s*([\d.]+)(\w+bits/s)\s+speed=\s*([\d.]+)x', line)
    if m:
        size_mib = round(int(m.group(1)) / 1024, 1)
        cur_secs = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4))
        bitrate_val = float(m.group(5))
        unit = m.group(6).lower()
        speed_multiplier = float(m.group(7))
        if unit.startswith('m'):
            bits_per_sec = bitrate_val * 1_000_000
        elif unit.startswith('k'):
            bits_per_sec = bitrate_val * 1_000
        else:
            bits_per_sec = bitrate_val
        # actual download speed = video bitrate × speed multiplier
        dl_speed_mib = round(bits_per_sec * speed_multiplier / 8 / 1_048_576, 1)
        result = {"file_size": f"{size_mib} MiB", "speed": f"{dl_speed_mib} MiB/s"}
        if total_secs and total_secs > 0:
            result["download_pct"] = round(min(cur_secs / total_secs * 100, 99.9), 1)
        return result
    return None


def parse_rclone_progress(line):
    if '"level"' not in line and '"stats"' not in line:
        return None
    try:
        data = json.loads(line)
        stats = data.get("stats", {})
        total = stats.get("totalBytes", 0)
        done = stats.get("bytes", 0)
        if total > 0:
            pct = round(done / total * 100, 1)
            eta_secs = stats.get("eta")
            speed_bps = stats.get("speed", 0)
            speed_str = ""
            if speed_bps:
                mb = speed_bps / 1_048_576
                speed_str = f"{mb:.1f} MiB/s"
            eta_str = ""
            if eta_secs is not None:
                m, s = divmod(int(eta_secs), 60)
                eta_str = f"{m:02d}:{s:02d}" if m else f"{s}s"
            return {"upload_pct": pct, "speed": speed_str, "eta": eta_str}
    except Exception:
        pass
    return None

def parse_aria2_progress(line):
    m = re.search(r'\[#\w+\s+[\d.]+\w*/[\d.]+\w*\(([\d.]+)%\).*DL:([\d.]+\w+)(?:\s+ETA:(\S+))?', line)
    if m:
        return {"download_pct": float(m.group(1)), "speed": m.group(2) + "/s", "eta": m.group(3) or ""}
    return None


def _emit_progress(job_id):
    """Push current job state to all connected WebSocket clients immediately."""
    try:
        j = db.get_job(job_id)
        if j:
            socketio.emit("job_update", {
                "id": j["id"], "status": j["status"],
                "download_pct": j.get("download_pct") or 0,
                "upload_pct":   j.get("upload_pct")   or 0,
                "upload_status": j.get("upload_status") or "",
                "speed":        j.get("speed")        or "",
                "eta":          j.get("eta")          or "",
                "file_size":    j.get("file_size")    or "",
                "name":         j.get("name")         or "",
            })
    except Exception:
        pass

# ── Source detection ──────────────────────────────────────────────────

_SOURCE_PATTERNS = [
    ("yt",  r'(youtube\.com|youtu\.be)'),
    ("fb",  r'(facebook\.com|fb\.watch|fb\.com)'),
    ("nm",  r'noodlemagazine\.com'),
    ("fh",  r'faphouse\.com'),
    ("vim", r'vimeo\.com'),
    ("tw",  r'(twitter\.com|x\.com)'),
    ("ig",  r'instagram\.com'),
    ("tt",  r'tiktok\.com'),
    ("twi", r'twitch\.tv'),
]

def detect_source(url):
    for name, pattern in _SOURCE_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return name
    return "direct"

# ── Job runners ───────────────────────────────────────────────────────

def run_download_job(job_id, url, name="", fmt="best", source="auto"):
    log_path = JOBS_DIR / f"{job_id}.log"
    db.update_job(job_id, status="running", started_at=datetime.utcnow().isoformat())

    def _log(msg):
        with open(log_path, "a") as f:
            f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    _log(f"Job started: {url}" + (f" -> {name}" if name else ""))
    try:
        container = docker_client.containers.get(QBT_CONTAINER)
        cmd = ["bash", "/scripts/download-url.sh", url]
        if name:
            cmd.append(name)
        env_list = [f"YT_FORMAT={fmt}"]
        # Map short source codes to provider script names
        _provider_map = {"yt": "youtube", "fb": "facebook", "nm": "noodlemagazine", "direct": "direct", "fh": "youtube"}
        if source and source not in ("auto", ""):
            provider = _provider_map.get(source, source)
            env_list.append(f"FORCE_PROVIDER={provider}")
        exec_id = docker_client.api.exec_create(container.id, cmd, environment=env_list)["Id"]
        stream = docker_client.api.exec_start(exec_id, stream=True)
        name_resolved = bool(name)  # True if user gave a custom name — don't override
        with open(log_path, "a") as f:
            for chunk in stream:
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    f.write(text)
                    f.flush()
                    for line in text.splitlines():
                        if "Uploading →" in line or "Uploading ->" in line:
                            db.update_job(job_id, status="uploading", upload_pct=0)
                        rclone = parse_rclone_progress(line)
                        if rclone:
                            db.update_job(job_id, status="uploading", **rclone)
                            _emit_progress(job_id)
                            continue
                        prog = parse_ytdlp_progress(line) or parse_aria2_progress(line)
                        if prog:
                            db.update_job(job_id, **prog)
                            _emit_progress(job_id)
                        # Detect output filename so the UI shows the real file name
                        # instead of the raw URL while the job is running / after done.
                        if not name_resolved:
                            detected = None
                            # yt-dlp final merged file (highest priority — definitive name)
                            m = re.search(r'\[(?:Merger|ffmpeg)\] Merging formats into "(.+)"', line)
                            if m:
                                detected = os.path.basename(m.group(1).strip())
                                name_resolved = True  # won't see a better name after this
                            # yt-dlp single-file download destination
                            elif '[download] Destination:' in line:
                                m = re.search(r'\[download\] Destination: (.+)', line)
                                if m:
                                    detected = os.path.basename(m.group(1).strip())
                            # direct provider — early filename echo
                            elif '[direct] Filename:' in line:
                                m = re.search(r'\[direct\] Filename: (.+)', line)
                                if m:
                                    detected = os.path.basename(m.group(1).strip())
                            # direct provider — completion echo
                            elif '[direct] Download complete:' in line:
                                m = re.search(r'\[direct\] Download complete: (.+)', line)
                                if m:
                                    detected = os.path.basename(m.group(1).strip())
                                    name_resolved = True
                            if detected:
                                db.update_job(job_id, name=detected)
        exit_code = docker_client.api.exec_inspect(exec_id)["ExitCode"]
        if exit_code == 0:
            db.update_job(job_id, status="done", download_pct=100, ended_at=datetime.utcnow().isoformat())
            _log("Done.")
        else:
            db.update_job(job_id, status="failed", ended_at=datetime.utcnow().isoformat(), error=f"Exit code {exit_code}")
            _log(f"Failed (exit {exit_code}).")
    except docker.errors.NotFound:
        db.update_job(job_id, status="failed", ended_at=datetime.utcnow().isoformat(), error="Container not found")
    except Exception as e:
        db.update_job(job_id, status="failed", ended_at=datetime.utcnow().isoformat(), error=str(e))


def _generate_contact_sheet(container, video_path, output_path, log_fn,
                             n_scenes=32, cols=4):
    """
    Generate an N-scene contact sheet using vcsi.
    vcsi handles: duration probe, even frame sampling, any aspect ratio
    (letterbox/pillarbox), timestamps on each tile, metadata header.
    Layout: cols × (n_scenes // cols) grid, total width 1920 px.
    Skips first and last 5% of the video to avoid intros/black frames.
    Returns True on success.
    """
    rows = n_scenes // cols
    cmd = [
        "vcsi", video_path,
        "-s", str(n_scenes),
        "-g", f"{cols}x{rows}",
        "--start-delay-percent", "5",
        "--end-delay-percent",   "5",
        "-w",                    "1920",
        "--quality",             "90",
        "--fast",
        "-o", output_path,
    ]
    result = container.exec_run(cmd)
    if result.exit_code == 0:
        log_fn(f"Contact sheet generated: {output_path.split('/')[-1]}")
        return True
    else:
        err = result.output.decode("utf-8", errors="replace")[-300:] if result.output else ""
        log_fn(f"Contact sheet failed (exit {result.exit_code}): {err}")
        return False


def run_fh_download(job_id, cdn_url, safe_name, info_name, info_payload):
    log_path = JOBS_DIR / f"{job_id}.log"
    db.update_job(job_id, status="downloading", started_at=datetime.utcnow().isoformat())

    def _log(msg):
        with open(log_path, "a") as f:
            f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    try:
        _log(f"Downloading: {safe_name}")
        _log(f"HLS URL: {cdn_url[:80]}...")
        container = docker_client.containers.get(QBT_CONTAINER)
        stem = safe_name[:-4]
        vid_dir = f"/downloads/faphouse/#moved/{stem}"
        container.exec_run(["mkdir", "-p", vid_dir])

        # Save JSON into the video folder
        info_bytes = json.dumps(info_payload, indent=2, ensure_ascii=False).encode("utf-8")
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            ti = tarfile.TarInfo(name=info_name)
            ti.size = len(info_bytes)
            tar.addfile(ti, io.BytesIO(info_bytes))
        tar_buf.seek(0)
        container.put_archive(vid_dir, tar_buf.getvalue())

        cmd = ["yt-dlp", "--no-playlist",
               "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
               "--merge-output-format", "mp4",
               "-o", f"{vid_dir}/{stem}.mp4",
               "--downloader", "ffmpeg",
               "--hls-use-mpegts",
               cdn_url]
        exec_id = docker_client.api.exec_create(container.id, cmd)["Id"]
        stream = docker_client.api.exec_start(exec_id, stream=True)
        fh_total_secs = None
        with open(log_path, "a") as f:
            for chunk in stream:
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    f.write(text)
                    f.flush()
                    for line in text.splitlines():
                        if fh_total_secs is None:
                            dm = re.search(r'Duration:\s+(\d+):(\d+):([\d.]+)', line)
                            if dm:
                                fh_total_secs = int(dm.group(1))*3600 + int(dm.group(2))*60 + float(dm.group(3))
                        prog = parse_ytdlp_progress(line) or parse_ffmpeg_progress(line, fh_total_secs)
                        if prog:
                            db.update_job(job_id, **prog)
                            _emit_progress(job_id)
        exit_code = docker_client.api.exec_inspect(exec_id)["ExitCode"]
        if exit_code == 0:
            _log(f"Download complete: {safe_name}")
            # Download thumbnail
            thumb_name = None
            thumbnail_url = info_payload.get("thumbnail_url", "")
            if thumbnail_url:
                try:
                    ext = thumbnail_url.split("?")[0].rsplit(".", 1)[-1].lower()
                    if ext not in ("jpg", "jpeg", "png", "webp"):
                        ext = "jpg"
                    thumb_name = stem + f".{ext}"
                    _log(f"Downloading thumbnail: {thumb_name}")
                    t_result = container.exec_run(
                        ["wget", "-q", "-O", f"{vid_dir}/{thumb_name}", thumbnail_url]
                    )
                    if t_result.exit_code != 0:
                        _log(f"Thumbnail download failed (exit {t_result.exit_code})")
                        thumb_name = None
                    else:
                        _log("Thumbnail downloaded.")
                except Exception as te:
                    _log(f"Thumbnail download error: {te}")
                    thumb_name = None
            # Generate contact sheet
            sheet_name = None
            _sheet_out = f"{vid_dir}/{stem}.preview.jpg"
            if _generate_contact_sheet(
                    container,
                    f"{vid_dir}/{stem}.mp4",
                    _sheet_out, _log):
                sheet_name = stem + ".preview.jpg"
            # ── Uploads ──────────────────────────────────────────────────
            try:
                c_info = docker_client.api.inspect_container(container.id)
                env_dict = dict(e.split("=", 1) for e in c_info["Config"]["Env"] if "=" in e)
                rclone_conf = env_dict.get("RCLONE_CONFIG", "/config/rclone/rclone.conf")

                # ── 1. OneDrive — upload the whole video folder ──
                od_remote  = env_dict.get("ONEDRIVE_REMOTE", "onedrive")
                od_path    = env_dict.get("WEBDL_PATH", "/WebDownloads")
                od_dest    = f"{od_remote}:{od_path}/faphouse/{stem}"
                db.update_job(job_id, status="uploading", upload_pct=0, upload_status="OneDrive")
                _emit_progress(job_id)
                _log(f"Uploading → OneDrive {od_dest}/")
                od_cmd = ["rclone", "copy",
                          vid_dir + "/",
                          od_dest,
                          "--config", rclone_conf,
                          "--retries", "10",
                          "--low-level-retries", "20",
                          "--no-check-dest",
                          "--use-json-log", "--stats=2s", "-v"]
                od_exec   = docker_client.api.exec_create(container.id, od_cmd)["Id"]
                od_stream = docker_client.api.exec_start(od_exec, stream=True)
                with open(log_path, "a") as f:
                    for chunk in od_stream:
                        if chunk:
                            text = chunk.decode("utf-8", errors="replace")
                            f.write(text); f.flush()
                            for line in text.splitlines():
                                rp = parse_rclone_progress(line)
                                if rp:
                                    db.update_job(job_id, status="uploading", **rp)
                                    _emit_progress(job_id)
                od_exit = docker_client.api.exec_inspect(od_exec)["ExitCode"]
                _log("OneDrive upload complete." if od_exit == 0 else f"OneDrive upload failed (exit {od_exit})")

                # ── 2. GCS Coldline — upload the whole video folder ──
                gcs_remote = env_dict.get("GCS_REMOTE", "gcs")
                gcs_bucket = env_dict.get("GCS_BUCKET", "cyberseed-bucket-01")
                gcs_dest   = f"{gcs_remote}:{gcs_bucket}/faphouse/{stem}"
                db.update_job(job_id, upload_pct=0, upload_status="GCS")
                _emit_progress(job_id)
                _log(f"Uploading → GCS {gcs_dest}/")
                gcs_cmd = ["rclone", "copy",
                           vid_dir + "/",
                           gcs_dest,
                           "--gcs-bucket-policy-only",
                           "--config", rclone_conf,
                           "--retries", "10",
                           "--low-level-retries", "20",
                           "--no-check-dest",
                           "--use-json-log", "--stats=2s", "-v"]
                gcs_exec   = docker_client.api.exec_create(container.id, gcs_cmd)["Id"]
                gcs_stream = docker_client.api.exec_start(gcs_exec, stream=True)
                with open(log_path, "a") as f:
                    for chunk in gcs_stream:
                        if chunk:
                            text = chunk.decode("utf-8", errors="replace")
                            f.write(text); f.flush()
                            for line in text.splitlines():
                                rp = parse_rclone_progress(line)
                                if rp:
                                    db.update_job(job_id, status="uploading", **rp)
                                    _emit_progress(job_id)
                gcs_exit = docker_client.api.exec_inspect(gcs_exec)["ExitCode"]
                if gcs_exit == 0:
                    _log("GCS upload complete.")
                    # Delete only the MP4 — JSON + thumbnail + preview stay
                    container.exec_run(["rm", "-f", f"{vid_dir}/{stem}.mp4"])
                    _log(f"MP4 deleted from #moved/{stem}/ (archived to GCS).")
                else:
                    _log(f"GCS upload failed (exit {gcs_exit}) — MP4 kept in place")
            except Exception as ue:
                _log(f"Upload error: {ue}")
            db.update_job(job_id, status="done", download_pct=100,
                          ended_at=datetime.utcnow().isoformat())
            _emit_progress(job_id)
        else:
            db.update_job(job_id, status="failed", ended_at=datetime.utcnow().isoformat(), error=f"yt-dlp exit {exit_code}")
            _log(f"yt-dlp exited with code {exit_code}")
    except Exception as e:
        db.update_job(job_id, status="failed", ended_at=datetime.utcnow().isoformat(), error=str(e))
        with open(log_path, "a") as f:
            f.write(f"\nERROR: {e}\n")

# ── HTTP routes ───────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html", error=None)

@app.route("/login", methods=["POST"])
def login_post():
    pw = request.form.get("password", "")
    if WEBUI_PASS and pw == WEBUI_PASS:
        session["logged_in"] = True
        return redirect(url_for("index"))
    return render_template("login.html", error="Wrong password.")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/")
@login_required
def index():
    return render_template("index.html")

# ── Jobs API ──────────────────────────────────────────────────────────

@app.route("/api/jobs")
@login_required
def api_jobs():
    source   = request.args.get("source", "all")
    status   = request.args.get("status", "all")
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))
    search   = request.args.get("search", "")
    jobs, total = db.list_jobs(source=source, status=status, page=page, per_page=per_page, search=search)
    return jsonify({"jobs": jobs, "total": total, "page": page, "per_page": per_page,
                    "pages": max(1, (total + per_page - 1) // per_page)})

@app.route("/api/jobs/stats")
@login_required
def api_stats():
    source = request.args.get("source", None)
    return jsonify(db.get_stats(source))

@app.route("/api/submit", methods=["POST"])
@login_required
def api_submit():
    data     = request.get_json(force=True) or {}
    urls_raw = data.get("urls", "")
    name     = data.get("name", "")
    fmt      = data.get("format", "best")
    source   = data.get("source", "auto")
    force    = data.get("force", False)

    lines = [l.strip() for l in urls_raw.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return jsonify({"error": "No URLs provided"}), 400

    parsed = []
    for line in lines:
        parts = line.split(None, 1)
        url   = parts[0]
        n     = parts[1] if len(parts) > 1 else name
        parsed.append((url, n))

    urls_only = [u for u, _ in parsed]
    if not force:
        dupes = db.find_duplicates(urls_only)
        if dupes:
            return jsonify({"duplicates": list(dupes), "total": len(urls_only),
                            "new": len(urls_only) - len(dupes),
                            "message": f"{len(dupes)} URL(s) already downloaded or in progress."}), 409

    submitted = []
    for url, n in parsed:
        detected = detect_source(url)
        job_id = str(uuid.uuid4())[:8]
        job = {"id": job_id, "url": url, "name": n.strip(),
               "source": detected if source in ("auto", "") else source,
               "quality": fmt.strip() or "best", "status": "queued",
               "created_at": datetime.utcnow().isoformat()}
        db.insert_job(job)
        submitted.append(job)
        threading.Thread(target=run_download_job,
                         args=(job_id, url, n.strip(), fmt, job["source"]), daemon=True).start()
    return jsonify({"submitted": len(submitted), "jobs": submitted})

@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def api_cancel(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    if job["status"] in ("done", "failed", "cancelled"):
        return jsonify({"error": "Already finished"}), 400
    db.update_job(job_id, status="cancelled", ended_at=datetime.utcnow().isoformat())
    return jsonify(db.get_job(job_id))

@app.route("/api/jobs/<job_id>", methods=["DELETE"])
@login_required
def api_delete_job(job_id):
    job = db.get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    db.delete_job(job_id)
    lp = JOBS_DIR / f"{job_id}.log"
    if lp.exists():
        lp.unlink()
    return jsonify({"deleted": job_id})

@app.route("/api/jobs", methods=["DELETE"])
@login_required
def api_clear_jobs():
    mode   = request.args.get("mode", "finished")
    source = request.args.get("source", None)
    ids = db.delete_jobs(mode=mode, source=source)
    for jid in ids:
        lp = JOBS_DIR / f"{jid}.log"
        if lp.exists():
            lp.unlink()
    return jsonify({"deleted": len(ids)})

@app.route("/api/jobs/<job_id>/log")
@login_required
def api_log_stream(job_id):
    log_path = JOBS_DIR / f"{job_id}.log"
    def generate():
        pos = 0
        while True:
            job = db.get_job(job_id)
            if log_path.exists():
                with open(log_path, "r") as f:
                    f.seek(pos)
                    chunk = f.read()
                    if chunk:
                        pos += len(chunk)
                        for line in chunk.splitlines():
                            yield f"data: {line}\n\n"
            if job and job["status"] in ("done", "failed", "cancelled"):
                yield "data: [DONE]\n\n"
                break
            time.sleep(0.5)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/jobs/<job_id>/log/full")
@login_required
def api_log_full(job_id):
    log_path = JOBS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return "No log yet.", 404
    return log_path.read_text(), 200, {"Content-Type": "text/plain"}

# ── SSE live progress ────────────────────────────────────────────────

@app.route("/api/progress")
@login_required
def api_progress_stream():
    source = request.args.get("source", None)
    def generate():
        while True:
            stats = db.get_stats(source)
            active, _ = db.list_jobs(source=source, per_page=100)
            active_jobs = [j for j in active if j["status"] in ("queued","running","downloading","pending","processing")]
            payload = {"stats": stats, "active": [{
                "id": j["id"], "name": j["name"], "url": j["url"],
                "source": j["source"], "status": j["status"],
                "download_pct": j.get("download_pct", 0),
                "upload_pct": j.get("upload_pct", 0),
                "speed": j.get("speed", ""), "eta": j.get("eta", ""),
                "file_size": j.get("file_size", ""),
            } for j in active_jobs]}
            yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(2)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── Duplicate check ──────────────────────────────────────────────────

@app.route("/api/check-duplicates", methods=["POST"])
@login_required
def api_check_dupes():
    data = request.get_json(force=True) or {}
    urls = data.get("urls", [])
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.splitlines() if u.strip()]
    return jsonify({"duplicates": list(db.find_duplicates(urls))})

# ── FapHouse extension API ────────────────────────────────────────────

@app.route("/api/faphouse/queue", methods=["POST"])
@login_required
def fh_queue_add():
    data = request.get_json(force=True) or {}
    raw  = data.get("urls", "")
    force = data.get("force", False)
    urls = [u.strip() for u in raw.strip().splitlines() if u.strip() and "faphouse.com" in u]
    if not urls:
        return jsonify({"error": "No valid faphouse URLs"}), 400
    if not force:
        dupes = db.find_duplicates(urls)
        if dupes:
            return jsonify({"duplicates": list(dupes), "total": len(urls),
                            "new": len(urls) - len(dupes),
                            "message": f"{len(dupes)} URL(s) already downloaded."}), 409
    added = []
    for url in urls:
        if not force and db.find_duplicates([url]):
            continue
        job_id = str(uuid.uuid4())[:8]
        job = {"id": job_id, "url": url, "name": "", "source": "fh",
               "quality": "1080", "status": "pending",
               "created_at": datetime.utcnow().isoformat()}
        db.insert_job(job)
        added.append(job)
    return jsonify({"added": len(added), "items": added})

@app.route("/api/faphouse/queue", methods=["GET"])
@login_required
def fh_queue_get():
    jobs, _ = db.list_jobs(source="fh", status="pending", per_page=50)
    return jsonify(jobs)

@app.route("/api/faphouse/queue/all", methods=["GET"])
@login_required
def fh_queue_all():
    page     = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 50))
    jobs, total = db.list_jobs(source="fh", page=page, per_page=per_page)
    return jsonify({"jobs": jobs, "total": total, "page": page,
                    "pages": max(1, (total + per_page - 1) // per_page)})

@app.route("/api/faphouse/queue/<item_id>/status", methods=["POST"])
@login_required
def fh_queue_status(item_id):
    data = request.get_json(force=True) or {}
    updates = {}
    if data.get("status"):
        updates["status"] = data["status"]
    if data.get("error"):
        updates["error"] = data["error"]
    if updates:
        db.update_job(item_id, **updates)
    return jsonify({"ok": True})

@app.route("/api/faphouse/resolve", methods=["POST"])
@login_required
def fh_resolve():
    data       = request.get_json(force=True) or {}
    item_id    = data.get("id", "")
    cdn_url    = data.get("cdn_url", "")
    title      = data.get("title", "") or "faphouse_video"
    quality    = data.get("quality", "")
    models     = data.get("models", [])
    studio     = data.get("studio", "")
    # Server-side safety filter: strip studio name from models regardless of
    # whether the extension already filtered it (service worker cache may be stale).
    if studio:
        studio_lower = studio.strip().lower()
        models = [m for m in models if m.strip().lower() != studio_lower]
    tags       = data.get("tags", [])
    duration   = data.get("duration", "")
    views      = data.get("views", "")
    published  = data.get("published", "")
    source_url    = data.get("source_url", "")
    thumbnail_url = data.get("thumbnail_url", "")

    if not cdn_url:
        return jsonify({"error": "No cdn_url"}), 400

    safe_name = re.sub(r'[^\w\s\-.]', '', title)[:200].strip() or "faphouse_video"
    if quality:
        q_tag = f"{quality}p" if quality.isdigit() else quality
        safe_name = f"{safe_name} [{q_tag}]"
    safe_name += ".mp4"
    info_name  = safe_name[:-4] + ".info.json"

    metadata = {"title": title, "source_url": source_url, "models": models,
                "studio": studio, "tags": tags, "duration": duration,
                "views": views, "published": published, "quality": quality,
                "cdn_url": cdn_url, "thumbnail_url": thumbnail_url,
                "downloaded_at": datetime.utcnow().isoformat()}

    db.update_job(item_id, status="downloading", name=safe_name, metadata=metadata)
    threading.Thread(target=run_fh_download,
                     args=(item_id, cdn_url, safe_name, info_name, metadata), daemon=True).start()
    return jsonify({"job_id": item_id, "name": safe_name})

@app.route("/api/faphouse/queue/clear", methods=["POST"])
@login_required
def fh_queue_clear():
    ids = db.delete_jobs(mode="finished", source="fh")
    for jid in ids:
        lp = JOBS_DIR / f"{jid}.log"
        if lp.exists():
            lp.unlink()
    return jsonify({"ok": True, "deleted": len(ids)})

# ── File API (minimal — filebrowser handles the rest) ─────────────────

def _safe_path(rel):
    clean = Path(rel.lstrip("/"))
    resolved = (DOWNLOADS_ROOT / clean).resolve()
    if not str(resolved).startswith(str(DOWNLOADS_ROOT.resolve())):
        return None
    return resolved

def _format_size(n):
    for unit in ("", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}B" if unit else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"

@app.route("/api/files")
@login_required
def api_files_list():
    rel    = request.args.get("path", "")
    target = _safe_path(rel)
    if target is None or not target.exists() or not target.is_dir():
        return jsonify({"error": "Invalid path"}), 400
    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            stat = entry.stat()
            is_dir = entry.is_dir()
            size = stat.st_size if not is_dir else 0
            items.append({"name": entry.name, "is_dir": is_dir,
                          "size": size, "size_human": _format_size(size),
                          "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()})
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    return jsonify({"path": rel or "/", "items": items})

@app.route("/api/files/download")
@login_required
def api_files_download():
    rel    = request.args.get("path", "")
    target = _safe_path(rel)
    if target is None or not target.exists() or not target.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(target, as_attachment=True, download_name=target.name)

@app.route("/api/files/delete", methods=["POST"])
@login_required
def api_files_delete():
    data   = request.get_json(force=True)
    rel    = data.get("path", "")
    target = _safe_path(rel)
    if target is None or not target.exists():
        return jsonify({"error": "Not found"}), 404
    if target.resolve() == DOWNLOADS_ROOT.resolve():
        return jsonify({"error": "Cannot delete root"}), 400
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"deleted": rel})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8888, allow_unsafe_werkzeug=True)
