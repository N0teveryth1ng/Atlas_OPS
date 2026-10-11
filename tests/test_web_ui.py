"""Web UI tests: upload limits, auth, locked pipeline, start guard."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from atlas import cli, dashboard
from atlas.dashboard import create_app
from atlas.db import connect, init_db, log_stage, save_profile, start_run, upsert_job
from atlas.schemas import CandidateProfile, Job


@pytest.fixture(autouse=True)
def _reset_pipeline():
    dashboard._pipeline_status.update(dashboard._blank_pipeline_status())
    dashboard._sessions.clear()
    yield
    dashboard._pipeline_status.update(dashboard._blank_pipeline_status())
    dashboard._sessions.clear()


def _client(monkeypatch, tmp_path, *, password: str | None):
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    return TestClient(create_app(tmp_path / "dash.db"))


def _logged_in(monkeypatch, tmp_path, password="pw"):
    client = _client(monkeypatch, tmp_path, password=password)
    client.post("/login", data={"password": password})
    return client


def test_login_required_on_every_route(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path, password="pw")
    for path in ("/", "/setup", "/profile", "/rejected", "/status"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login", path


def test_health_needs_no_login(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path, password="pw")
    assert client.get("/health").status_code == 200


def test_upload_requires_login(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path, password="pw")
    response = client.post(
        "/setup",
        files={"resume": ("x.pdf", b"%PDF-1.4", "application/pdf")},
        data={"description": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_upload_size_limit(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path, password="pw")
    client.post("/login", data={"password": "pw"})
    big = b"%PDF-1.4" + b"x" * (dashboard.MAX_UPLOAD_BYTES + 1)
    response = client.post(
        "/setup",
        files={"resume": ("big.pdf", big, "application/pdf")},
        data={"description": ""},
    )
    assert "too large" in response.text


def test_upload_type_limit(tmp_path, monkeypatch):
    client = _client(monkeypatch, tmp_path, password="pw")
    client.post("/login", data={"password": "pw"})
    response = client.post(
        "/setup",
        files={"resume": ("notes.txt", b"hello", "text/plain")},
        data={"description": ""},
    )
    assert "PDF" in response.text


def test_upload_ignores_supplied_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(dashboard, "DATA_DIR", data_dir)
    captured = {}

    def fake_build(*, resume_path=None, description=None, database=None):
        captured["resume_path"] = resume_path

    monkeypatch.setattr(dashboard, "build_profile", fake_build)
    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.post(
        "/setup",
        files={"resume": ("../../../evil.pdf", b"%PDF-1.4", "application/pdf")},
        data={"description": ""},
    )
    assert response.status_code == 200 or response.status_code == 303
    assert captured["resume_path"] is not None
    assert "evil" not in str(captured["resume_path"])
    assert str(captured["resume_path"]).endswith("resume.pdf")
    # The uploaded file is deleted after parsing.
    assert not (dashboard.DATA_DIR / "resume.pdf").exists()


def test_pipeline_blocked_without_approved_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    dashboard._run_pipeline_worker(tmp_path / "dash.db")
    assert dashboard._pipeline_status["stage"] == "blocked"
    assert "profile" in dashboard._pipeline_status["errors"]


def test_only_one_run_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    started = {"count": 0, "done": 0}

    def slow(*args, **kwargs):
        started["count"] += 1
        time.sleep(0.2)
        started["done"] += 1

    monkeypatch.setattr(dashboard, "_run_pipeline_worker", slow)
    assert dashboard.start_pipeline(tmp_path / "dash.db") is True
    assert dashboard.start_pipeline(tmp_path / "dash.db") is False
    time.sleep(0.5)
    assert started["count"] == 1


def test_results_exclude_skip_and_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    keep, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/1", dedupe_key="a1", title="Keep")
    )
    skip, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/2", dedupe_key="a2", title="SkipMe")
    )
    partial, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/3", dedupe_key="a3", title="Partial")
    )
    log_stage(conn, "filter_results", job_id=keep, passed=True)
    log_stage(conn, "filter_results", job_id=skip, passed=False)
    # A job with both a pass and a fail is partially filtered -> excluded.
    log_stage(conn, "filter_results", job_id=partial, passed=True)
    log_stage(conn, "filter_results", job_id=partial, passed=False)
    conn.commit()
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/")
    assert "Keep" in response.text
    assert "SkipMe" not in response.text
    assert "Partial" not in response.text


def test_rejected_page_shows_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/9", dedupe_key="a9", title="Too Senior")
    )
    log_stage(
        conn,
        "filter_results",
        job_id=job_id,
        passed=False,
        rule_id="seniority",
        evidence="7+ years",
    )
    conn.commit()
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/rejected")
    assert "Too Senior" in response.text
    assert "seniority" in response.text


def test_rejected_page_shows_what_was_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/10", dedupe_key="a10", title="Too Senior")
    )
    log_stage(
        conn,
        "filter_results",
        job_id=job_id,
        passed=False,
        rule_id="senior_title",
        evidence="title seniority=senior",
        data={
            "checks": [
                {
                    "rule_id": "location_mismatch",
                    "checked": False,
                    "skipped_reason": "no target locations set",
                },
                {"rule_id": "stale_job", "checked": False, "skipped_reason": "posted date unknown"},
            ]
        },
    )
    conn.commit()
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/rejected")
    assert "location_mismatch: skipped - no target locations set" in response.text
    assert "stale_job: skipped - posted date unknown" in response.text


def test_refuse_non_loopback_without_password(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(RuntimeError):
        create_app(tmp_path / "dash.db", host="0.0.0.0")


def test_non_loopback_with_password_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    assert create_app(tmp_path / "dash.db", host="0.0.0.0") is not None


def test_find_reports_per_stage_and_per_source(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    save_profile(conn, CandidateProfile(full_name="A", target_roles=["x"], approved=True))
    conn.close()

    def fake_execute(args, *, run_kind="run", on_event=None):
        assert on_event is not None
        on_event({"stage": "collecting"})
        on_event({"stage": "collected", "collected": 3, "new_jobs": 2})
        on_event(
            {
                "stage": "processing",
                "total": 3,
                "done": 3,
                "parsed": 3,
                "filtered_out": 1,
                "needs_review": 0,
                "evaluated": 2,
                "verified": 2,
                "decided": 2,
            }
        )
        on_event(
            {
                "stage": "sources",
                "sources": [
                    {
                        "source": "remotive",
                        "fetched": 5,
                        "kept": 3,
                        "passed_filters": 2,
                        "sent": 0,
                    }
                ],
            }
        )
        return 0

    monkeypatch.setattr(cli, "_execute_run", fake_execute)
    dashboard._run_pipeline_worker(db)

    status = dashboard._pipeline_status
    assert status["stage"] == "done"
    assert status["stages"]["decided"] == 2
    assert status["stages"]["filtered_out"] == 1
    assert status["sources"][0]["source"] == "remotive"


def test_find_status_page_shows_stages_and_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    dashboard._pipeline_status["stage"] = "processing"
    dashboard._pipeline_status["stages"] = {"decided": 2, "filtered_out": 1}
    dashboard._pipeline_status["sources"] = [
        {"source": "remotive", "fetched": 5, "kept": 3, "passed_filters": 2, "sent": 0}
    ]
    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/status")
    assert response.status_code == 200
    assert "Per-stage counts" in response.text
    assert "Per-source results" in response.text
    assert "remotive" in response.text
