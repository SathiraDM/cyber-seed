#!/usr/bin/env python3
"""
cyber-seed Web UI
Dispatches download-url.sh jobs to the qbt container via Docker SDK.
Streams live progress via Server-Sent Events.
"""

import docker
import functools
import hashlib
import io
import json
import os
import re
import tarfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, stream_with_context, url_for)

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────
JOBS_DIR       = Path("/logs/jobs")
JOBS_INDEX     = Path("/logs/jobs/index.json")
DOWNLOADS_ROOT = Path(os.environ.get("DOWNLOADS_ROOT", "/downloads"))
QBT_CONTAINER  = os.environ.get("QBT_CONTAINER", "cyber-seed-qbt")
WEBUI_PASS     = os.environ.get("QBT_WEBUI_PASS", "")
# Derive a stable secret from the password so sessions survive restarts
app.secret_key = hashlib.sha256(f"cyber-seed:{WEBUI_PASS}".encode()).hexdigest()
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# ── CORS (for Chrome extension) ───────────────────────────────────────
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
        # Allow API key auth for extension calls
        api_key = request.headers.get("X-Api-Key", "")
        if api_key and api_key == WEBUI_PASS:
            return f(*args, **kwargs)
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ── Docker client ─────────────────────────────────────────────────────
try:
    docker_client = docker.from_env()
except Exception as e:
    print(f"[webui] WARNING: Docker client error: {e}")
    docker_client = None

# ── Job persistence ───────────────────────────────────────────────────
_jobs_lock = threading.Lock()

def load_jobs() -> dict:
    try:
        if JOBS_INDEX.exists():
            return json.loads(JOBS_INDEX.read_text())
    except Exception:
        pass
    return {}

def save_jobs(jobs: dict):
    JOBS_INDEX.write_text(json.dumps(jobs, indent=2))

def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return load_jobs().get(job_id)

def upsert_job(job: dict):
    with _jobs_lock:
        jobs = load_jobs()
        jobs[job["id"]] = job
        save_jobs(jobs)

def all_jobs() -> list:
    with _jobs_lock:
        jobs = load_jobs()
    return sorted(jobs.values(), key=lambda j: j.get("started_at", ""), reverse=True)

# ── Job runner ────────────────────────────────────────────────────────
def run_job(job: dict):
    job_id   = job["id"]
    url      = job["url"]
    name     = job.get("name") or ""
    log_path = JOBS_DIR / f"{job_id}.log"

    job["status"]     = "running"
    job["started_at"] = datetime.utcnow().isoformat()
    upsert_job(job)

    def _log(msg):
        with open(log_path, "a") as f:
            f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    _log(f"Job started: {url}" + (f" → {name}" if name else ""))

    try:
        container = docker_client.containers.get(QBT_CONTAINER)
        cmd = ["bash", "/scripts/download-url.sh", url]
        if name:
            cmd.append(name)

        fmt = job.get("format", "best")
        forced_src = job.get("source", "auto")
        env = {"YT_FORMAT": fmt}
        if forced_src and forced_src not in ("auto", ""):
            env["FORCE_PROVIDER"] = forced_src
        # Convert env dict to list of KEY=VALUE strings for low-level API
        env_list = [f"{k}={v}" for k, v in env.items()]
        # Use low-level API for proper streaming + exit code
        exec_id = docker_client.api.exec_create(
            container.id, cmd, environment=env_list,
        )["Id"]
        stream = docker_client.api.exec_start(exec_id, stream=True)

        with open(log_path, "a") as f:
            for chunk in stream:
                if chunk:
                    f.write(chunk.decode("utf-8", errors="replace"))
                    f.flush()

        exit_code = docker_client.api.exec_inspect(exec_id)["ExitCode"]

        if exit_code == 0:
            job["status"] = "done"
            _log("✓ Completed successfully.")
        else:
            job["status"] = "failed"
            _log(f"✗ Failed with exit code {exit_code}.")

    except docker.errors.NotFound:
        job["status"] = "failed"
        _log(f"ERROR: Container '{QBT_CONTAINER}' not found.")
    except Exception as e:
        job["status"] = "failed"
        _log(f"ERROR: {e}")

    job["ended_at"] = datetime.utcnow().isoformat()
    upsert_job(job)


# ── Source detection ─────────────────────────────────────────────────
_SOURCE_PATTERNS = [
    ("youtube",        r'(youtube\.com|youtu\.be)'),
    ("facebook",       r'(facebook\.com|fb\.watch|fb\.com)'),
    ("noodlemagazine", r'noodlemagazine\.com'),
    ("faphouse",       r'faphouse\.com'),
    ("vimeo",          r'vimeo\.com'),
    ("twitter",        r'(twitter\.com|x\.com)'),
    ("instagram",      r'instagram\.com'),
    ("tiktok",         r'tiktok\.com'),
    ("twitch",         r'twitch\.tv'),
]

def detect_source(url: str) -> str:
    for name, pattern in _SOURCE_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return name
    return "direct"


def submit_job(url: str, name: str = "", fmt: str = "best", source_override: str = "auto") -> dict:
    detected = detect_source(url.strip())
    job = {
        "id":         str(uuid.uuid4())[:8],
        "url":        url.strip(),
        "name":       name.strip(),
        "source":     detected if source_override in ("auto", "") else source_override,
        "format":     fmt.strip() or "best",
        "status":     "queued",
        "started_at": datetime.utcnow().isoformat(),
        "ended_at":   None,
    }
    upsert_job(job)
    t = threading.Thread(target=run_job, args=(job,), daemon=True)
    t.start()
    return job

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

@app.route("/api/jobs")
@login_required
def api_jobs():
    return jsonify(all_jobs())

@app.route("/api/submit", methods=["POST"])
@login_required
def api_submit():
    data = request.get_json(force=True) or {}
    urls_raw  = data.get("urls", "")
    name      = data.get("name", "")

    fmt    = data.get("format", "best")
    source = data.get("source", "auto")
    lines = [l.strip() for l in urls_raw.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return jsonify({"error": "No URLs provided"}), 400

    jobs = []
    for line in lines:
        parts = line.split(None, 1)
        url   = parts[0]
        n     = parts[1] if len(parts) > 1 else name
        jobs.append(submit_job(url, n, fmt, source))

    return jsonify({"submitted": len(jobs), "jobs": jobs})

@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
@login_required
def api_cancel(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    if job["status"] in ("done", "failed", "cancelled"):
        return jsonify({"error": "Already finished"}), 400
    job["status"] = "cancelled"
    job["ended_at"] = datetime.utcnow().isoformat()
    upsert_job(job)
    return jsonify(job)

@app.route("/api/jobs/<job_id>/log")
@login_required
def api_log_stream(job_id):
    """Server-Sent Events: stream job log file live."""
    log_path = JOBS_DIR / f"{job_id}.log"

    def generate():
        pos = 0
        while True:
            job = get_job(job_id)
            if log_path.exists():
                with open(log_path, "r") as f:
                    f.seek(pos)
                    chunk = f.read()
                    if chunk:
                        pos += len(chunk)
                        # Send each line as SSE data
                        for line in chunk.splitlines():
                            yield f"data: {line}\n\n"
            if job and job["status"] in ("done", "failed", "cancelled"):
                yield "data: [DONE]\n\n"
                break
            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@app.route("/api/jobs/<job_id>/log/full")
@login_required
def api_log_full(job_id):
    log_path = JOBS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return "No log yet.", 404
    return log_path.read_text(), 200, {"Content-Type": "text/plain"}

@app.route("/api/jobs/<job_id>", methods=["DELETE"])
@login_required
def api_delete_job(job_id):
    with _jobs_lock:
        jobs = load_jobs()
        if job_id not in jobs:
            return jsonify({"error": "Not found"}), 404
        del jobs[job_id]
        save_jobs(jobs)
    log_path = JOBS_DIR / f"{job_id}.log"
    if log_path.exists():
        log_path.unlink()
    return jsonify({"deleted": job_id})

@app.route("/api/jobs", methods=["DELETE"])
@login_required
def api_clear_jobs():
    mode = request.args.get("mode", "finished")  # finished | all
    with _jobs_lock:
        jobs = load_jobs()
        if mode == "all":
            to_delete = list(jobs.keys())
        else:
            to_delete = [jid for jid, j in jobs.items()
                         if j.get("status") in ("done", "failed", "cancelled")]
        for jid in to_delete:
            del jobs[jid]
            log_path = JOBS_DIR / f"{jid}.log"
            if log_path.exists():
                log_path.unlink()
        save_jobs(jobs)
    return jsonify({"deleted": len(to_delete)})

# ── File manager ──────────────────────────────────────────────────────
def _safe_path(rel: str) -> Path:
    """Resolve a relative path under DOWNLOADS_ROOT, rejecting traversal."""
    clean = Path(rel.lstrip("/"))
    resolved = (DOWNLOADS_ROOT / clean).resolve()
    if not str(resolved).startswith(str(DOWNLOADS_ROOT.resolve())):
        return None
    return resolved

def _dir_size(p: Path) -> int:
    """Recursively sum file sizes under a directory."""
    total = 0
    try:
        for f in p.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except Exception:
        pass
    return total

def _format_size(n: int) -> str:
    for unit in ("", "K", "M", "G", "T"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}B" if unit else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"

@app.route("/files")
@login_required
def files_page():
    return render_template("files.html")

@app.route("/api/files")
@login_required
def api_files_list():
    rel = request.args.get("path", "")
    target = _safe_path(rel)
    if target is None or not target.exists():
        return jsonify({"error": "Invalid path"}), 400
    if not target.is_dir():
        return jsonify({"error": "Not a directory"}), 400

    items = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            stat = entry.stat()
            is_dir = entry.is_dir()
            items.append({
                "name": entry.name,
                "is_dir": is_dir,
                "size": _dir_size(entry) if is_dir else stat.st_size,
                "size_human": _format_size(_dir_size(entry) if is_dir else stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    return jsonify({"path": rel or "/", "items": items})

@app.route("/api/files/download")
@login_required
def api_files_download():
    rel = request.args.get("path", "")
    target = _safe_path(rel)
    if target is None or not target.exists() or not target.is_file():
        return jsonify({"error": "File not found"}), 404
    return send_file(target, as_attachment=True, download_name=target.name)

@app.route("/api/files/delete", methods=["POST"])
@login_required
def api_files_delete():
    data = request.get_json(force=True)
    rel = data.get("path", "")
    target = _safe_path(rel)
    if target is None or not target.exists():
        return jsonify({"error": "Not found"}), 404
    # Don't allow deleting the root downloads folder itself
    if target.resolve() == DOWNLOADS_ROOT.resolve():
        return jsonify({"error": "Cannot delete root"}), 400
    import shutil
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"deleted": rel})

# ── FapHouse Queue ────────────────────────────────────────────────────
FH_QUEUE  = Path("/logs/faphouse-queue.json")
_fh_lock  = threading.Lock()

def load_fh_queue() -> list:
    try:
        if FH_QUEUE.exists():
            return json.loads(FH_QUEUE.read_text())
    except Exception:
        pass
    return []

def save_fh_queue(q: list):
    FH_QUEUE.write_text(json.dumps(q, indent=2))


@app.route("/api/faphouse/queue", methods=["POST"])
@login_required
def fh_queue_add():
    """Add faphouse URLs to the queue. Body: {urls: "url1\nurl2\n..."}."""
    data = request.get_json(force=True) or {}
    raw  = data.get("urls", "")
    urls = [u.strip() for u in raw.strip().splitlines() if u.strip() and "faphouse.com" in u]
    if not urls:
        return jsonify({"error": "No valid faphouse URLs"}), 400

    added = []
    with _fh_lock:
        q = load_fh_queue()
        existing_urls = {item["url"] for item in q}
        for url in urls:
            if url in existing_urls:
                continue
            item = {
                "id":      str(uuid.uuid4())[:8],
                "url":     url,
                "status":  "pending",
                "title":   "",
                "error":   "",
                "created_at": datetime.utcnow().isoformat(),
            }
            q.append(item)
            added.append(item)
        save_fh_queue(q)

    return jsonify({"added": len(added), "items": added})


@app.route("/api/faphouse/queue", methods=["GET"])
@login_required
def fh_queue_get():
    """Return pending queue items (for the extension to process)."""
    with _fh_lock:
        q = load_fh_queue()
    pending = [i for i in q if i["status"] == "pending"]
    return jsonify(pending)


@app.route("/api/faphouse/queue/all", methods=["GET"])
@login_required
def fh_queue_all():
    """Return all queue items with status."""
    with _fh_lock:
        q = load_fh_queue()
    return jsonify(q)


@app.route("/api/faphouse/queue/<item_id>/status", methods=["POST"])
@login_required
def fh_queue_status(item_id):
    """Extension updates item status (processing / failed)."""
    data = request.get_json(force=True) or {}
    new_status = data.get("status", "")
    error_msg  = data.get("error", "")
    with _fh_lock:
        q = load_fh_queue()
        for item in q:
            if item["id"] == item_id:
                item["status"] = new_status
                if error_msg:
                    item["error"] = error_msg
                break
        save_fh_queue(q)
    return jsonify({"ok": True})


@app.route("/api/faphouse/resolve", methods=["POST"])
@login_required
def fh_resolve():
    """Extension sends resolved CDN URL + metadata. Backend downloads it."""
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
    rating     = data.get("rating", "")
    is_trailer = data.get("is_trailer", False)
    source_url = data.get("source_url", "")

    if not cdn_url:
        return jsonify({"error": "No cdn_url"}), 400

    # Sanitise filename
    safe_name = re.sub(r'[^\w\s\-.]', '', title)[:200].strip() or "faphouse_video"
    if quality:
        safe_name = f"{safe_name} [{quality}]"
    safe_name += ".mp4"
    info_name  = safe_name[:-4] + ".info.json"   # same stem, .info.json

    # Update queue item
    with _fh_lock:
        q = load_fh_queue()
        for item in q:
            if item["id"] == item_id:
                item["status"] = "downloading"
                item["title"]  = title
                break
        save_fh_queue(q)

    # Create a job and download in background
    job_id   = str(uuid.uuid4())[:8]
    log_path = JOBS_DIR / f"{job_id}.log"
    job = {
        "id":         job_id,
        "url":        cdn_url,
        "name":       safe_name,
        "source":     "faphouse",
        "format":     quality or "best",
        "status":     "running",
        "started_at": datetime.utcnow().isoformat(),
        "ended_at":   None,
    }
    upsert_job(job)

    def _download():
        try:
            with open(log_path, "a") as f:
                f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Downloading: {safe_name}\n")
                if is_trailer:
                    f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] WARNING: Only trailer URL found — user may not be premium on this video\n")
                f.write(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] CDN URL: {cdn_url[:80]}...\n")
                f.flush()

            container = docker_client.containers.get(QBT_CONTAINER)

            # Write .info.json into /downloads/faphouse/ before starting download
            info_payload = {
                "title":        title,
                "source_url":   source_url,
                "models":       models,
                "studio":       studio,
                "tags":         tags,
                "duration":     duration,
                "views":        views,
                "rating":       rating,
                "quality":      quality,
                "is_trailer":   is_trailer,
                "cdn_url":      cdn_url,
                "downloaded_at": datetime.utcnow().isoformat(),
            }
            container.exec_run(["mkdir", "-p", "/downloads/faphouse"])
            info_bytes = json.dumps(info_payload, indent=2, ensure_ascii=False).encode("utf-8")
            tar_buf = io.BytesIO()
            with tarfile.open(fileobj=tar_buf, mode="w") as tar:
                ti = tarfile.TarInfo(name=info_name)
                ti.size = len(info_bytes)
                tar.addfile(ti, io.BytesIO(info_bytes))
            tar_buf.seek(0)
            container.put_archive("/downloads/faphouse", tar_buf.getvalue())

            cmd = [
                "aria2c",
                "--dir=/downloads/faphouse",
                f"--out={safe_name}",
                "--max-connection-per-server=16",
                "--split=16",
                "--min-split-size=1M",
                "--summary-interval=5",
                "--console-log-level=notice",
                cdn_url,
            ]
            exec_id = docker_client.api.exec_create(container.id, cmd)["Id"]
            stream  = docker_client.api.exec_start(exec_id, stream=True)

            with open(log_path, "a") as f:
                for chunk in stream:
                    if chunk:
                        f.write(chunk.decode("utf-8", errors="replace"))
                        f.flush()

            exit_code = docker_client.api.exec_inspect(exec_id)["ExitCode"]

            if exit_code == 0:
                job["status"] = "done"
                with open(log_path, "a") as f:
                    f.write(f"\n✓ Download complete: {safe_name}\n")
            else:
                job["status"] = "failed"
                with open(log_path, "a") as f:
                    f.write(f"\n✗ aria2c exited with code {exit_code}\n")

        except Exception as e:
            job["status"] = "failed"
            with open(log_path, "a") as f:
                f.write(f"\nERROR: {e}\n")

        job["ended_at"] = datetime.utcnow().isoformat()
        upsert_job(job)

        # Update queue item status
        with _fh_lock:
            q_ = load_fh_queue()
            for it in q_:
                if it["id"] == item_id:
                    it["status"] = "done" if job["status"] == "done" else "failed"
                    break
            save_fh_queue(q_)

    threading.Thread(target=_download, daemon=True).start()
    return jsonify({"job_id": job_id, "name": safe_name})


@app.route("/api/faphouse/queue/clear", methods=["POST"])
@login_required
def fh_queue_clear():
    """Remove completed/failed items from queue."""
    with _fh_lock:
        q = load_fh_queue()
        q = [i for i in q if i["status"] in ("pending", "processing", "downloading")]
        save_fh_queue(q)
    return jsonify({"ok": True})


# ── Browser ───────────────────────────────────────────────────────────
@app.route("/browser")
@login_required
def browser_page():
    return render_template("browser.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888, threaded=True)
