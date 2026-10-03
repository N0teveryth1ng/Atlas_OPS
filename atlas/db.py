"""SQLite persistence layer.

One file (``atlas.db`` by default) holds every stage's inputs and outputs so a
run is resumable and replayable. Standard library ``sqlite3`` only.

``init_db`` is idempotent; call it once at startup.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import REPO_ROOT
from .schemas import CandidateProfile, Job

DEFAULT_DB_PATH = REPO_ROOT / "atlas.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running',
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    approved    INTEGER NOT NULL DEFAULT 0,
    data_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    query      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES runs(id),
    source       TEXT NOT NULL,
    source_id    TEXT,
    payload_json TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES runs(id),
    source          TEXT NOT NULL,
    source_id       TEXT,
    dedupe_key      TEXT UNIQUE,
    title           TEXT,
    company         TEXT,
    location        TEXT,
    remote_type     TEXT,
    url             TEXT NOT NULL,
    urls_json       TEXT,
    description_raw TEXT,
    posted_at       TEXT,
    fetched_at      TEXT,
    emailed_at      TEXT
);

CREATE TABLE IF NOT EXISTS parsed_jds (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER REFERENCES jobs(id),
    model          TEXT,
    prompt_version TEXT,
    created_at     TEXT NOT NULL,
    data_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filter_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER REFERENCES jobs(id),
    passed     INTEGER NOT NULL,
    rule_id    TEXT,
    evidence   TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER REFERENCES jobs(id),
    model          TEXT,
    prompt_version TEXT,
    created_at     TEXT NOT NULL,
    data_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER REFERENCES jobs(id),
    model          TEXT,
    prompt_version TEXT,
    created_at     TEXT NOT NULL,
    data_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER REFERENCES runs(id),
    created_at TEXT NOT NULL,
    top_k      INTEGER,
    html       TEXT,
    text       TEXT,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER REFERENCES jobs(id),
    verdict     TEXT NOT NULL,
    reason_code TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    override = os.environ.get("ATLAS_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first released schema (idempotent)."""
    job_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "emailed_at" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN emailed_at TEXT")
    digest_cols = {row["name"] for row in conn.execute("PRAGMA table_info(digests)")}
    if "text" not in digest_cols:
        conn.execute("ALTER TABLE digests ADD COLUMN text TEXT")


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #


def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, status, started_at) VALUES (?, 'running', ?)",
        (kind, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, status: str, summary: dict[str, Any] | None = None
) -> None:
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, summary_json = ? WHERE id = ?",
        (status, _now(), json.dumps(summary) if summary is not None else None, run_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def save_profile(conn: sqlite3.Connection, profile: CandidateProfile) -> int:
    cur = conn.execute(
        "INSERT INTO profiles (created_at, source_mode, approved, data_json) "
        "VALUES (?, ?, ?, ?)",
        (
            _now(),
            profile.source_mode.value,
            int(profile.approved),
            profile.model_dump_json(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_latest_profile(conn: sqlite3.Connection) -> CandidateProfile | None:
    row = conn.execute(
        "SELECT data_json FROM profiles ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return CandidateProfile.model_validate_json(row["data_json"])


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


def upsert_job(conn: sqlite3.Connection, run_id: int, job: Job) -> tuple[int, bool]:
    """Insert a job, or return the existing id if its dedupe_key is present.

    Returns ``(job_id, created)``.
    """
    if job.dedupe_key:
        existing = conn.execute(
            "SELECT id, urls_json FROM jobs WHERE dedupe_key = ?", (job.dedupe_key,)
        ).fetchone()
        if existing:
            merged = set(json.loads(existing["urls_json"] or "[]"))
            merged.update(job.urls)
            merged.add(job.url)
            conn.execute(
                "UPDATE jobs SET urls_json = ? WHERE id = ?",
                (json.dumps(sorted(merged)), existing["id"]),
            )
            conn.commit()
            return int(existing["id"]), False

    cur = conn.execute(
        """
        INSERT INTO jobs (run_id, source, source_id, dedupe_key, title, company,
                          location, remote_type, url, urls_json, description_raw,
                          posted_at, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            job.source,
            job.source_id,
            job.dedupe_key,
            job.title,
            job.company,
            job.location,
            job.remote_type.value,
            job.url,
            json.dumps(job.urls or [job.url]),
            job.description_raw,
            job.posted_at.isoformat() if job.posted_at else None,
            job.fetched_at.isoformat() if job.fetched_at else _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid), True


def job_seen(conn: sqlite3.Connection, dedupe_key: str) -> bool:
    row = conn.execute("SELECT 1 FROM jobs WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Digests + email idempotency
# --------------------------------------------------------------------------- #


def emailed_job_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT id FROM jobs WHERE emailed_at IS NOT NULL").fetchall()
    return {int(row["id"]) for row in rows}


def mark_jobs_emailed(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    if not job_ids:
        return
    placeholders = ",".join("?" * len(job_ids))
    conn.execute(
        f"UPDATE jobs SET emailed_at = ? WHERE id IN ({placeholders})",
        (_now(), *job_ids),
    )
    conn.commit()


def save_digest(
    conn: sqlite3.Connection,
    run_id: int | None,
    top_k: int,
    html: str,
    text: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO digests (run_id, created_at, top_k, html, text) VALUES (?, ?, ?, ?, ?)",
        (run_id, _now(), top_k, html, text),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_digest_sent(conn: sqlite3.Connection, digest_id: int) -> None:
    conn.execute("UPDATE digests SET sent_at = ? WHERE id = ?", (_now(), digest_id))
    conn.commit()


# --------------------------------------------------------------------------- #
# Generic stage logging
# --------------------------------------------------------------------------- #


def log_stage(
    conn: sqlite3.Connection,
    table: str,
    *,
    job_id: int | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    data: dict[str, Any] | None = None,
    passed: bool | None = None,
    rule_id: str | None = None,
    evidence: str | None = None,
) -> int:
    """Record a stage result. Keeps every stage's output traceable."""
    if table == "filter_results":
        cur = conn.execute(
            "INSERT INTO filter_results (job_id, passed, rule_id, evidence, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, int(bool(passed)), rule_id, evidence, _now()),
        )
    else:
        cur = conn.execute(
            f"INSERT INTO {table} (job_id, model, prompt_version, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (job_id, model, prompt_version, _now(), json.dumps(data or {})),
        )
    conn.commit()
    return int(cur.lastrowid)
