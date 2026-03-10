"""
SQLite database layer for CyberSeed.
Single `jobs` table — unified across all sources (YT, NM, FH, Direct, Browser).
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DB_PATH = Path("/logs/cyberseed.db")

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """One connection per thread (SQLite isn't thread-safe per connection)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            name        TEXT DEFAULT '',
            source      TEXT DEFAULT 'direct',
            quality     TEXT DEFAULT 'best',
            status      TEXT DEFAULT 'queued',
            progress    REAL DEFAULT 0,
            speed       TEXT DEFAULT '',
            eta         TEXT DEFAULT '',
            file_size   TEXT DEFAULT '',
            download_pct REAL DEFAULT 0,
            upload_pct  REAL DEFAULT 0,
            upload_status TEXT DEFAULT '',
            metadata    TEXT DEFAULT '{}',
            log_tail    TEXT DEFAULT '',
            error       TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            started_at  TEXT DEFAULT '',
            ended_at    TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
    """)
    conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if d.get("metadata"):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except Exception:
            d["metadata"] = {}
    return d


# ── CRUD ──────────────────────────────────────────────────────────────

def insert_job(job: dict):
    conn = _get_conn()
    meta = job.get("metadata", {})
    if isinstance(meta, dict):
        meta = json.dumps(meta, ensure_ascii=False)
    conn.execute("""
        INSERT INTO jobs (id, url, name, source, quality, status, metadata, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        job["id"], job["url"], job.get("name", ""), job.get("source", "direct"),
        job.get("quality", "best"), job.get("status", "queued"),
        meta, job.get("created_at", datetime.utcnow().isoformat()),
    ))
    conn.commit()


def update_job(job_id: str, **fields):
    if not fields:
        return
    conn = _get_conn()
    if "metadata" in fields and isinstance(fields["metadata"], dict):
        fields["metadata"] = json.dumps(fields["metadata"], ensure_ascii=False)
    sets = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", vals)
    conn.commit()


def get_job(job_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row)


def list_jobs(source: str = None, status: str = None,
              page: int = 1, per_page: int = 20,
              search: str = None) -> tuple[list[dict], int]:
    """Return (jobs_list, total_count) with pagination + optional filters."""
    conn = _get_conn()
    where, params = [], []

    if source and source != "all":
        where.append("source = ?")
        params.append(source)
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if search:
        where.append("(url LIKE ? OR name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    count = conn.execute(
        f"SELECT COUNT(*) FROM jobs {where_clause}", params
    ).fetchone()[0]

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT * FROM jobs {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    return [_row_to_dict(r) for r in rows], count


def find_duplicates(urls: list[str]) -> set[str]:
    """Return set of URLs that have already been downloaded (status=done)."""
    if not urls:
        return set()
    conn = _get_conn()
    placeholders = ",".join("?" * len(urls))
    rows = conn.execute(
        f"SELECT DISTINCT url FROM jobs WHERE url IN ({placeholders}) AND status IN ('done', 'running', 'queued', 'downloading')",
        urls,
    ).fetchall()
    return {r["url"] for r in rows}


def delete_job(job_id: str):
    conn = _get_conn()
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()


def delete_jobs(mode: str = "finished", source: str = None):
    """Delete jobs by mode: 'finished' (done/failed/cancelled) or 'all'."""
    conn = _get_conn()
    where, params = [], []
    if mode != "all":
        where.append("status IN ('done', 'failed', 'cancelled')")
    if source and source != "all":
        where.append("source = ?")
        params.append(source)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Get IDs first for log cleanup
    rows = conn.execute(f"SELECT id FROM jobs {where_clause}", params).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", ids)
        conn.commit()
    return ids


def get_stats(source: str = None) -> dict:
    """Get counts by status, optionally filtered by source."""
    conn = _get_conn()
    where, params = [], []
    if source and source != "all":
        where.append("source = ?")
        params.append(source)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT status, COUNT(*) as cnt FROM jobs {where_clause} GROUP BY status",
        params,
    ).fetchall()
    stats = {"queued": 0, "running": 0, "downloading": 0, "done": 0, "failed": 0, "cancelled": 0, "total": 0}
    for r in rows:
        stats[r["status"]] = r["cnt"]
        stats["total"] += r["cnt"]
    return stats
