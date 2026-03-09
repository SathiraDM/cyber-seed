#!/usr/bin/env python3
"""
cyber-seed Web UI
Dispatches download-url.sh jobs to the qbt container via Docker SDK.
Streams live progress via Server-Sent Events.
"""

import docker
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────
JOBS_DIR       = Path("/logs/jobs")
JOBS_INDEX     = Path("/logs/jobs/index.json")
QBT_CONTAINER  = os.environ.get("QBT_CONTAINER", "cyber-seed-qbt")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

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
        exit_code, output = container.exec_run(
            cmd,
            stream=False,
            demux=False,
            environment={"YT_FORMAT": fmt},
        )
        output_text = output.decode("utf-8", errors="replace") if output else ""
        with open(log_path, "a") as f:
            f.write(output_text)

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


def submit_job(url: str, name: str = "", fmt: str = "best") -> dict:
    job = {
        "id":         str(uuid.uuid4())[:8],
        "url":        url.strip(),
        "name":       name.strip(),
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

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/jobs")
def api_jobs():
    return jsonify(all_jobs())

@app.route("/api/submit", methods=["POST"])
def api_submit():
    data = request.get_json(force=True) or {}
    urls_raw  = data.get("urls", "")
    name      = data.get("name", "")

    fmt   = data.get("format", "best")
    lines = [l.strip() for l in urls_raw.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        return jsonify({"error": "No URLs provided"}), 400

    jobs = []
    for line in lines:
        parts   = line.split(None, 1)
        url     = parts[0]
        n       = parts[1] if len(parts) > 1 else name
        jobs.append(submit_job(url, n, fmt))

    return jsonify({"submitted": len(jobs), "jobs": jobs})

@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
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
def api_log_full(job_id):
    log_path = JOBS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return "No log yet.", 404
    return log_path.read_text(), 200, {"Content-Type": "text/plain"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8888, threaded=True)
