"""Database layer tests: profiles, jobs, digests, run summary and recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from atlas.db import (
    connect,
    emailed_job_ids,
    fail_stale_runs,
    get_latest_profile,
    init_db,
    job_seen,
    latest_run,
    mark_digest_sent,
    mark_jobs_emailed,
    save_digest,
    save_profile,
    start_run,
    update_run_summary,
    upsert_job,
)
from atlas.schemas import CandidateProfile, Job


def _conn(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_db(conn)
    return conn


def test_profile_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    profile = CandidateProfile(full_name="X", target_roles=["Backend"], approved=True)
    save_profile(conn, profile)

    loaded = get_latest_profile(conn)
    assert loaded is not None
    assert loaded.full_name == "X"
    assert loaded.approved is True


def test_upsert_job_dedupes_by_key(tmp_path):
    conn = _conn(tmp_path)
    run_id = start_run(conn, "test")

    first = Job(source="a", url="http://x", dedupe_key="k", title="T")
    id1, created1 = upsert_job(conn, run_id, first)
    second = Job(source="b", url="http://y", dedupe_key="k", title="T")
    id2, created2 = upsert_job(conn, run_id, second)

    assert created1 is True
    assert created2 is False
    assert id1 == id2
    assert job_seen(conn, "k")

    row = conn.execute("SELECT urls_json FROM jobs WHERE id = ?", (id1,)).fetchone()
    assert "http://x" in row["urls_json"]
    assert "http://y" in row["urls_json"]


def test_emailed_jobs_are_tracked_for_idempotency(tmp_path):
    conn = _conn(tmp_path)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(conn, run_id, Job(source="a", url="http://x", dedupe_key="k", title="T"))

    assert emailed_job_ids(conn) == set()
    mark_jobs_emailed(conn, [job_id])
    assert emailed_job_ids(conn) == {job_id}


def test_digest_roundtrip(tmp_path):
    conn = _conn(tmp_path)
    digest_id = save_digest(conn, run_id=None, top_k=10, html="<b>hi</b>", text="hi")
    assert digest_id > 0
    mark_digest_sent(conn, digest_id)
    row = conn.execute("SELECT sent_at FROM digests WHERE id = ?", (digest_id,)).fetchone()
    assert row["sent_at"] is not None


def test_update_run_summary_keeps_run_running(tmp_path):
    conn = _conn(tmp_path)
    run_id = start_run(conn, "test")

    update_run_summary(conn, run_id, {"stage": "processing", "stages": {"parsed": 3}})

    row = conn.execute(
        "SELECT status, finished_at, summary_json FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "running"
    assert row["finished_at"] is None
    assert json.loads(row["summary_json"]) == {"stage": "processing", "stages": {"parsed": 3}}

    latest = latest_run(conn)
    assert latest is not None
    assert latest["id"] == run_id


def test_fail_stale_runs_marks_only_old_running_runs(tmp_path):
    conn = _conn(tmp_path)
    old = start_run(conn, "old")
    recent = start_run(conn, "recent")
    done = start_run(conn, "done")
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", old))
    conn.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (done,))
    conn.commit()

    failed = fail_stale_runs(conn, now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC))
    assert failed == [old]

    old_row = conn.execute(
        "SELECT status, finished_at, summary_json FROM runs WHERE id = ?", (old,)
    ).fetchone()
    summary = json.loads(old_row["summary_json"])
    assert old_row["status"] == "failed"
    assert old_row["finished_at"] is not None
    assert summary["status"] == "failed"
    assert "more than 30 minutes" in summary["error"]

    recent_row = conn.execute("SELECT status FROM runs WHERE id = ?", (recent,)).fetchone()
    done_row = conn.execute("SELECT status FROM runs WHERE id = ?", (done,)).fetchone()
    assert recent_row["status"] == "running"
    assert done_row["status"] == "ok"


def test_fail_stale_runs_keeps_existing_summary(tmp_path):
    conn = _conn(tmp_path)
    run_id = start_run(conn, "old")
    update_run_summary(conn, run_id, {"stage": "collecting", "stages": {"collected": 4}})
    conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", run_id)
    )
    conn.commit()

    failed = fail_stale_runs(conn, now=datetime(2026, 1, 1, 2, 0, tzinfo=UTC))

    assert failed == [run_id]
    summary = json.loads(
        conn.execute("SELECT summary_json FROM runs WHERE id = ?", (run_id,)).fetchone()[
            "summary_json"
        ]
    )
    assert summary["stages"] == {"collected": 4}
    assert summary["status"] == "failed"
