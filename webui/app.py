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

# ── WebSocket push thread ─────────────────────────────────────────────
# Pushes a 'refresh' event to all connected clients.
# Runs fast (1s) when active jobs exist, slow (8s) when idle.

def _jobs_push_thread():
    ACTIVE_STATUSES = {"running", "downloading", "uploading", "processing", "queued", "pending"}
    while True:
        try:
            jobs = db.list_jobs(source="", status="", page=1, per_page=200, search="")
            has_active = any(j["status"] in ACTIVE_STATUSES for j in jobs["jobs"])
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

# ── Progress parsing ──────────────────────────────────────────────────

def parse_ytdlp_progress(line):
    m = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+~?([\d.]+\s*\w+)\s+at\s+([\d.]+\s*\w+/s)\s+ETA\s+(\S+)', line)
    if m:
        return {"download_pct": float(m.group(1)), "file_size": m.group(2).strip(),
                "speed": m.group(3).strip(), "eta": m.group(4).strip()}
    m2 = re.search(r'\[download\]\s+([\d.]+)%\s+of\s+~?([\d.]+\s*\w+)\s+at\s+([\d.]+\s*\w+/s)', line)
    if m2:
        return {"download_pct": float(m2.group(1)), "file_size": m2.group(2).strip(),
                "speed": m2.group(3).strip()}
    if "[download] 100%" in line:
        return {"download_pct": 100.0}
    return None

def parse_rclone_progress(line):
    """Parse rclone JSON log line for upload progress."""
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


    m = re.search(r'\[#\w+\s+[\d.]+\w*/[\d.]+\w*\(([\d.]+)%\).*DL:([\d.]+\w+)(?:\s+ETA:(\S+))?', line)
    if m:
        return {"download_pct": float(m.group(1)), "speed": m.group(2) + "/s", "eta": m.group(3) or ""}
    return None

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
        _provider_map = {"yt": "youtube", "fb": "facebook", "nm": "noodlemagazine", "direct": "direct"}
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
                            continue
                        prog = parse_ytdlp_progress(line) or parse_aria2_progress(line)
                        if prog:
                            db.update_job(job_id, **prog)
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
        container.exec_run(["mkdir", "-p", "/downloads/faphouse"])
        info_bytes = json.dumps(info_payload, indent=2, ensure_ascii=False).encode("utf-8")
        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            ti = tarfile.TarInfo(name=info_name)
            ti.size = len(info_bytes)
            tar.addfile(ti, io.BytesIO(info_bytes))
        tar_buf.seek(0)
        container.put_archive("/downloads/faphouse", tar_buf.getvalue())

        cmd = ["yt-dlp", "--no-playlist",
               "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
               "--merge-output-format", "mp4",
               "-o", f"/downloads/faphouse/{safe_name[:-4]}.mp4",
               "--no-part", cdn_url]
        exec_id = docker_client.api.exec_create(container.id, cmd)["Id"]
        stream = docker_client.api.exec_start(exec_id, stream=True)
        with open(log_path, "a") as f:
            for chunk in stream:
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    f.write(text)
                    f.flush()
                    for line in text.splitlines():
                        prog = parse_ytdlp_progress(line)
                        if prog:
                            db.update_job(job_id, **prog)
        exit_code = docker_client.api.exec_inspect(exec_id)["ExitCode"]
        if exit_code == 0:
            db.update_job(job_id, status="done", download_pct=100, ended_at=datetime.utcnow().isoformat())
            _log(f"Download complete: {safe_name}")
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
    tags       = data.get("tags", [])
    duration   = data.get("duration", "")
    views      = data.get("views", "")
    published  = data.get("published", "")
    source_url = data.get("source_url", "")

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
                "cdn_url": cdn_url, "downloaded_at": datetime.utcnow().isoformat()}

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
