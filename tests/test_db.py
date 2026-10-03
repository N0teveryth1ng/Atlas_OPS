from atlas.db import (
    connect,
    emailed_job_ids,
    get_latest_profile,
    init_db,
    job_seen,
    mark_digest_sent,
    mark_jobs_emailed,
    save_digest,
    save_profile,
    start_run,
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
