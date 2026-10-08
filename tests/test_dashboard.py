"""Dashboard tests: matched-job listing, status updates, safe rendering."""

from __future__ import annotations

from argparse import Namespace

import pytest
from fastapi.testclient import TestClient

from atlas import cli
from atlas.dashboard import create_app
from atlas.db import (
    connect,
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


def _add_job(conn, run_id, *, url, title="Junior Python Developer", passed: bool):
    job_id, _ = upsert_job(conn, run_id, Job(source="test", url=url, dedupe_key=url, title=title))
    log_stage(conn, "filter_results", job_id=job_id, passed=passed)
    return job_id


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
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: started.update(kwargs))

    rc = cli.cmd_dashboard(Namespace(host="0.0.0.0", port=9000))
    assert rc == 0
    assert "no authentication" in capsys.readouterr().err
    assert started["host"] == "0.0.0.0"

    rc = cli.cmd_dashboard(Namespace(host="127.0.0.1", port=8000))
    assert rc == 0
    assert "no authentication" not in capsys.readouterr().err
