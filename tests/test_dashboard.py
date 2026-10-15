"""Dashboard tests: matched-job listing, status updates, safe rendering."""

from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from atlas import cli
from atlas import dashboard as dash
from atlas.dashboard import create_app
from atlas.db import (
    add_llm_tokens,
    connect,
    finish_run,
    get_latest_profile,
    init_db,
    log_stage,
    matched_jobs,
    set_application_status,
    start_run,
    upsert_job,
)
from atlas.schemas import Job


def _connect(tmp_path):
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    return conn


def _add_job(conn, run_id, *, url, title="Junior Python Developer", source="test", passed: bool):
    job_id, _ = upsert_job(conn, run_id, Job(source=source, url=url, dedupe_key=url, title=title))
    log_stage(conn, "filter_results", job_id=job_id, passed=passed)
    return job_id


def _decide(conn, job_id, *, outcome, match_score=80):
    log_stage(
        conn,
        "decisions",
        job_id=job_id,
        data={
            "outcome": outcome,
            "match_score": match_score,
            "reasons_for": ["strong skill overlap"],
            "reasons_against": [],
            "missing_skills": [],
        },
    )


def test_matched_jobs_lists_only_filter_passes(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    matched_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    _add_job(conn, run_id, url="http://x/2", passed=False)
    _add_job(conn, run_id, url="http://x/3", passed=False)

    rows = matched_jobs(conn)
    assert [int(row["id"]) for row in rows] == [matched_id]
    conn.close()


def test_application_status_defaults_to_pending_then_updates(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(conn, run_id, Job(source="test", url="http://x/1", dedupe_key="k1"))
    row = conn.execute("SELECT application_status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["application_status"] == "PEND"

    set_application_status(conn, job_id, "APLD")
    row = conn.execute("SELECT application_status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["application_status"] == "APLD"

    with pytest.raises(ValueError):
        set_application_status(conn, job_id, "MAYBE")
    with pytest.raises(KeyError):
        set_application_status(conn, 9999, "APLD")
    conn.close()


def test_migration_restores_application_status_column(tmp_path):
    conn = _connect(tmp_path)
    conn.execute("ALTER TABLE jobs DROP COLUMN application_status")
    conn.commit()
    conn.close()

    reopened = connect(tmp_path / "dash.db")
    init_db(reopened)
    columns = {row["name"] for row in reopened.execute("PRAGMA table_info(jobs)")}
    assert "application_status" in columns
    reopened.close()


def test_index_lists_matched_jobs_and_escapes_titles(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    _add_job(conn, run_id, url="http://x/1", title="Junior <b>Python</b>", passed=True)
    _add_job(conn, run_id, url="http://x/2", title="Hidden Role", passed=False)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.get("/")
    assert response.status_code == 200
    assert "Junior &lt;b&gt;Python&lt;/b&gt;" in response.text
    assert "<b>Python</b>" not in response.text
    assert "Hidden Role" not in response.text


def test_index_never_links_non_http_schemes(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    _add_job(conn, run_id, url="javascript:alert(1)", title="Fancy Job", passed=True)
    conn.close()

    response = TestClient(create_app(tmp_path / "dash.db")).get("/")
    assert response.status_code == 200
    assert 'href="javascript:' not in response.text
    assert "Fancy Job" in response.text


def test_status_update_redirects_and_persists(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.post(f"/jobs/{job_id}/status/APLD", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    conn = connect(tmp_path / "dash.db")
    row = conn.execute("SELECT application_status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    assert row["application_status"] == "APLD"


def test_status_update_rejects_unknown_status_and_job(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    assert client.post(f"/jobs/{job_id}/status/MAYBE").status_code == 400
    assert client.post("/jobs/9999/status/APLD").status_code == 404


def test_status_update_rejects_cross_origin_post(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.post(
        f"/jobs/{job_id}/status/APLD",
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403

    conn = connect(tmp_path / "dash.db")
    row = conn.execute("SELECT application_status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    assert row["application_status"] == "PEND"


def test_status_update_allows_same_origin_post(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.post(
        f"/jobs/{job_id}/status/NTAPLD",
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_api_matched_returns_json(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    payload = client.get("/api/matched").json()
    assert [item["id"] for item in payload] == [job_id]
    assert payload[0]["application_status"] == "PEND"


def test_health_endpoint(tmp_path):
    client = TestClient(create_app(tmp_path / "dash.db"))
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_database_degrades_gracefully(tmp_path):
    client = TestClient(create_app(tmp_path / "no" / "such" / "dir" / "dash.db"))
    page = client.get("/")
    assert page.status_code == 200
    assert "Database unavailable" in page.text
    assert client.get("/api/matched").status_code == 503


def test_dashboard_parser_exposes_host_and_port():
    args = cli.build_parser().parse_args(["dashboard", "--host", "0.0.0.0", "--port", "9001"])
    assert args.func is cli.cmd_dashboard
    assert args.host == "0.0.0.0"
    assert args.port == 9001


def test_dashboard_warns_only_for_non_loopback_hosts(monkeypatch, capsys):
    import uvicorn

    started = {}
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(kwargs))

    rc = cli.cmd_dashboard(Namespace(host="0.0.0.0", port=9000))
    assert rc == 0
    assert "WARNING" in capsys.readouterr().err
    assert started["host"] == "0.0.0.0"

    rc = cli.cmd_dashboard(Namespace(host="127.0.0.1", port=8000))
    assert rc == 0
    assert "WARNING" not in capsys.readouterr().err


def test_dashboard_refuses_non_loopback_without_password(monkeypatch):
    import uvicorn

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        cli.cmd_dashboard(Namespace(host="0.0.0.0", port=9000))


def test_index_shows_only_apply_and_review_decisions(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    apply_id = _add_job(conn, run_id, url="http://x/apply", title="Apply Role", passed=True)
    review_id = _add_job(conn, run_id, url="http://x/review", title="Review Role", passed=True)
    skip_id = _add_job(conn, run_id, url="http://x/skip", title="Skip Role", passed=True)
    _decide(conn, apply_id, outcome="apply")
    _decide(conn, review_id, outcome="review")
    _decide(conn, skip_id, outcome="skip")
    conn.close()

    text = TestClient(create_app(tmp_path / "dash.db")).get("/").text
    assert "Apply Role" in text
    assert "Review Role" in text
    assert "Skip Role" not in text


def test_index_lists_undecided_and_hides_manual_rows(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    _add_job(conn, run_id, url="http://x/u", title="Undecided Role", passed=True)
    _add_job(conn, run_id, url="http://x/m", title="Manual Role", source="manual", passed=True)
    conn.close()

    text = TestClient(create_app(tmp_path / "dash.db")).get("/").text
    assert "Not yet evaluated (1)" in text
    assert "Undecided Role" in text
    assert "Manual Role" not in text


def test_index_shows_stored_outcome_and_flags_unknown_seniority(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", title="Mystery Role", passed=True)
    log_stage(conn, "parsed_jds", job_id=job_id, data={"title_seniority": "unknown"})
    _decide(conn, job_id, outcome="apply")
    conn.close()

    text = TestClient(create_app(tmp_path / "dash.db")).get("/").text
    # The engine now stores REVIEW for unknown seniority; the dashboard shows the
    # stored outcome verbatim (no display-only downgrade) and keeps the warning.
    assert "Mystery Role" in text
    assert "seniority unknown" in text
    assert ">APPLY " in text


def test_profile_approval_requires_a_location(tmp_path):
    _connect(tmp_path).close()
    client = TestClient(create_app(tmp_path / "dash.db"))

    blocked = client.post(
        "/profile",
        data={
            "level": "junior",
            "months": "12",
            "skills": "python:strong",
            "target_roles": "backend",
            "locations": "",
            "ambiguities": "",
        },
        follow_redirects=False,
    )
    assert blocked.status_code == 422
    assert "at least one location" in blocked.text

    conn = connect(tmp_path / "dash.db")
    assert get_latest_profile(conn) is None
    conn.close()

    ok = client.post(
        "/profile",
        data={
            "level": "junior",
            "months": "12",
            "skills": "python:strong",
            "target_roles": "backend",
            "locations": "anywhere",
            "ambiguities": "",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303

    conn = connect(tmp_path / "dash.db")
    profile = get_latest_profile(conn)
    conn.close()
    assert profile is not None
    assert profile.approved is True
    assert profile.locations == ["anywhere"]


def test_kit_page_requires_login(tmp_path, monkeypatch):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    _decide(conn, job_id, outcome="apply")
    conn.close()

    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.get(f"/jobs/{job_id}/kit", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_kit_page_404_for_non_shippable_job(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    failed_id = _add_job(conn, run_id, url="http://x/failed", passed=False)
    undecided_id = _add_job(conn, run_id, url="http://x/undecided", passed=True)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    assert client.get(f"/jobs/{failed_id}/kit").status_code == 404
    assert client.get(f"/jobs/{undecided_id}/kit").status_code == 404
    assert client.get("/jobs/9999/kit").status_code == 404


def test_kit_page_renders_for_shippable_decided_job(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    _decide(conn, job_id, outcome="review")
    conn.close()

    response = TestClient(create_app(tmp_path / "dash.db")).get(f"/jobs/{job_id}/kit")
    assert response.status_code == 200
    assert "Application kit" in response.text


def test_kit_post_rejects_cross_origin(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1", passed=True)
    _decide(conn, job_id, outcome="apply")
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.post(f"/jobs/{job_id}/kit", headers={"origin": "http://evil.example"})
    assert response.status_code == 403


def test_index_hides_undecided_jobs_older_than_the_max_age(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    fresh_id, _ = upsert_job(
        conn, run_id, Job(source="test", url="http://x/fresh", dedupe_key="f", title="Fresh Role")
    )
    log_stage(conn, "filter_results", job_id=fresh_id, passed=True)
    old_id, _ = upsert_job(
        conn,
        run_id,
        Job(
            source="test",
            url="http://x/old",
            dedupe_key="o",
            title="Stale Role",
            posted_at=datetime.now(UTC) - timedelta(days=40),
        ),
    )
    log_stage(conn, "filter_results", job_id=old_id, passed=True)
    conn.close()

    text = TestClient(create_app(tmp_path / "dash.db")).get("/").text
    assert "Fresh Role" in text
    assert "Stale Role" not in text
    assert "Not yet evaluated (1)" in text


def test_profile_approval_reopens_rejected_jobs_the_profile_now_passes(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn,
        run_id,
        Job(
            source="test",
            url="http://x/berlin",
            dedupe_key="berlin",
            title="Backend Engineer",
            location="Berlin",
            remote_type="onsite",
            description_raw="Backend Engineer in our Berlin office.",
        ),
    )
    log_stage(
        conn,
        "filter_results",
        job_id=job_id,
        passed=False,
        rule_id="location_mismatch",
        evidence="location 'Berlin' not in ['Pune']",
    )
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", ("rejected", job_id))
    conn.commit()
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    response = client.post(
        "/profile",
        data={
            "level": "junior",
            "months": "12",
            "skills": "python:strong",
            "target_roles": "backend",
            "locations": "Berlin",
            "ambiguities": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    conn = connect(tmp_path / "dash.db")
    status = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
    event = conn.execute(
        "SELECT event, reason FROM job_events WHERE job_id = ?", (job_id,)
    ).fetchone()
    conn.close()
    assert status == "new"
    assert event["event"] == "reopened_for_refilter"
    assert event["reason"] == "profile approved"


def _with_status(**fields):
    """Swap in a canned live pipeline status, restoring the global afterwards."""
    return _StatusOverride(fields)


class _StatusOverride:
    def __init__(self, fields):
        self._fields = fields
        self._saved = None

    def __enter__(self):
        self._saved = dict(dash._pipeline_status)
        dash._pipeline_status.update(self._fields)
        return self

    def __exit__(self, *exc):
        dash._pipeline_status.clear()
        dash._pipeline_status.update(self._saved)
        return False


def test_status_shows_live_job_step_calls_waiting_and_eta(tmp_path):
    import time

    with _with_status(
        running=True,
        stage="processing",
        started_at=time.time() - 30,
        finished_at=None,
        recovered_run_id=None,
        progress={
            "total": 10,
            "done": 5,
            "current_job_id": 42,
            "current_job_title": "Senior Widget Engineer",
            "current_step": "evaluate",
            "llm_calls": 3,
            "llm_calls_cap": 100,
            "rate_limit_wait": 12.0,
            "rate_limit_reason": "rate limit",
        },
    ):
        text = TestClient(create_app(tmp_path / "dash.db")).get("/status").text

    assert "Current job: 42 — Senior Widget Engineer" in text
    assert "Step: evaluate" in text
    assert "LLM calls: 3 / 100" in text
    assert "Waiting for Groq" in text
    assert "retry in 12" in text
    assert "ETA: ~" in text


def test_status_hides_restored_banner_while_a_run_is_active(tmp_path):
    with _with_status(running=True, stage="processing", recovered_run_id=33):
        text = TestClient(create_app(tmp_path / "dash.db")).get("/status").text
    assert "restored from the database" not in text
    assert "last recorded run #33" not in text

    with _with_status(running=False, stage="failed", recovered_run_id=33, result="failed"):
        text = TestClient(create_app(tmp_path / "dash.db")).get("/status").text
    assert "last recorded run #33" in text
    assert "restored from the database" in text


def test_status_shows_paused_daily_limit_message(tmp_path):
    with _with_status(
        running=False,
        stage="paused",
        result="paused",
        recovered_run_id=None,
        progress={
            "paused": True,
            "pause_reason": "daily",
            "pause_message": "Paused: Groq daily limit reached, resets in about 5.0h",
            "pause_reset_seconds": 18000.0,
        },
    ):
        text = TestClient(create_app(tmp_path / "dash.db")).get("/status").text

    assert "Paused: Groq daily limit reached, resets in about 5.0h" in text


def test_status_shows_tokens_used_today(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    add_llm_tokens(conn, 12345)
    conn.close()

    with _with_status(running=False, stage="done", recovered_run_id=None):
        text = TestClient(create_app(db)).get("/status").text

    assert "Tokens used today" in text
    assert "12,345" in text


def _seed_paused_run(conn, *, reason="daily", reset_seconds=3600.0, finished=None):
    run_id = start_run(conn, "run")
    finish_run(
        conn,
        run_id,
        "paused",
        summary={
            "stage": "paused",
            "result": "paused",
            "pause_reason": reason,
            "pause_message": "Paused: Groq daily limit reached, resets in about 1.0h",
            "pause_reset_seconds": reset_seconds,
        },
    )
    if finished is not None:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (finished, run_id))
        conn.commit()
    return run_id


def _daily_limit() -> int:
    from atlas.config import get_settings

    return get_settings().run.max_llm_tokens_per_day


def test_groq_daily_limit_message_none_when_under_threshold(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    add_llm_tokens(conn, 1000)
    conn.close()

    assert dash.groq_daily_limit_message(db) is None


def test_groq_daily_limit_message_when_tokens_reach_90_percent(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    add_llm_tokens(conn, int(_daily_limit() * 0.9))
    conn.close()

    message = dash.groq_daily_limit_message(db)
    assert message is not None
    assert message.startswith("Groq daily limit reached, resets in about ")
    assert message.endswith("h")


def test_groq_daily_limit_message_when_pause_still_in_effect(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    _seed_paused_run(conn)
    conn.close()

    message = dash.groq_daily_limit_message(db)
    assert message is not None
    assert "resets in about" in message


def test_groq_daily_limit_message_clears_after_pause_window(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    past = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    _seed_paused_run(conn, reset_seconds=3600.0, finished=past)
    conn.close()

    assert dash.groq_daily_limit_message(db) is None


def test_groq_daily_limit_message_ignores_non_daily_pause(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    _seed_paused_run(conn, reason="tokens")
    conn.close()

    assert dash.groq_daily_limit_message(db) is None


def test_find_refuses_when_daily_limit_reached(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    add_llm_tokens(conn, _daily_limit())
    conn.close()

    with _with_status(running=False, stage="idle", recovered_run_id=None):
        response = TestClient(create_app(db)).post("/find")

    assert response.status_code == 200
    assert "Groq daily limit reached, resets in about" in response.text
    assert dash._pipeline_status["running"] is False
