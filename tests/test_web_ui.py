"""Web UI tests: upload limits, auth, locked pipeline, start guard."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fake_llm import make_client
from fastapi.testclient import TestClient

from atlas import cli, dashboard, sourcing
from atlas.collectors.base import Collector
from atlas.dashboard import create_app
from atlas.db import (
    connect,
    init_db,
    log_stage,
    save_profile,
    start_run,
    update_run_summary,
    upsert_job,
)
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


def test_rejected_page_marks_cheap_rejections(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/13", dedupe_key="a13", title="Too Senior")
    )
    log_stage(
        conn,
        "filter_results",
        job_id=job_id,
        passed=False,
        rule_id="senior_title",
        evidence="title seniority=senior",
        data={"checks": [], "cheap": True},
    )
    conn.commit()
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/rejected")
    assert "senior_title" in response.text
    assert "cheap" in response.text


def _recorded_filter_date(conn, job_id):
    conn.execute(
        "UPDATE filter_results SET created_at = ? WHERE job_id = ?",
        ("2026-10-07T16:53:12+00:00", job_id),
    )
    conn.commit()


def test_rejected_page_shows_when_the_filter_ran(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/11", dedupe_key="a11", title="Stale Pick")
    )
    log_stage(conn, "filter_results", job_id=job_id, passed=False, rule_id="seniority")
    _recorded_filter_date(conn, job_id)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/rejected")
    assert "Stale Pick" in response.text
    assert "2026-10-07 16:53 UTC" in response.text


def test_undecided_page_shows_when_the_filter_ran(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn, run_id, Job(source="s", url="http://a/12", dedupe_key="a12", title="Unsettled")
    )
    log_stage(conn, "filter_results", job_id=job_id, passed=True)
    _recorded_filter_date(conn, job_id)
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/")
    assert "Unsettled" in response.text
    assert "2026-10-07 16:53 UTC" in response.text


def test_startup_marks_stale_run_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "dashboard")
    started = datetime.now(UTC) - timedelta(hours=2)
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (started.isoformat(), run_id))
    conn.commit()
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/status")
    assert response.status_code == 200
    assert "failed" in response.text

    conn = connect(tmp_path / "dash.db")
    row = conn.execute("SELECT status, finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None


def test_startup_reraises_no_run_and_stays_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    TestClient(create_app(tmp_path / "dash.db"))
    assert dashboard._pipeline_status["stage"] == "idle"
    assert dashboard._pipeline_status["running"] is False


def test_startup_restores_last_run_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "dashboard")
    update_run_summary(
        conn,
        run_id,
        {
            "stage": "processing",
            "stages": {"parsed": 4, "decided": 2},
            "sources": [
                {"source": "remotive", "fetched": 5, "kept": 3, "passed_filters": 2, "sent": 0}
            ],
            "errors": {"remotive": "timeout"},
        },
    )
    conn.close()

    client = TestClient(create_app(tmp_path / "dash.db"))
    client.post("/login", data={"password": "pw"})
    response = client.get("/status")
    assert "remotive" in response.text
    assert "decided: 2" in response.text
    assert f"run #{run_id}" in response.text
    assert "restored from the database" in response.text


def test_startup_does_not_clobber_a_running_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    dashboard._pipeline_status["running"] = True
    dashboard._pipeline_status["stage"] = "processing"
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "dashboard")
    started = datetime.now(UTC) - timedelta(hours=2)
    conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (started.isoformat(), run_id))
    conn.commit()
    conn.close()

    TestClient(create_app(tmp_path / "dash.db"))

    assert dashboard._pipeline_status["stage"] == "processing"
    assert dashboard._pipeline_status["running"] is True
    conn = connect(tmp_path / "dash.db")
    status = conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()["status"]
    conn.close()
    # Recovery did not run because a live pipeline owns the status; the stale row
    # is left for the next real startup.
    assert status == "running"


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

    def fake_execute(args, *, run_kind="run", on_event=None, on_source=None, database=None):
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


class _HangingCollector(Collector):
    query_based = True

    def __init__(self, name: str):
        super().__init__(get_json=lambda url, params: None)
        self.name = name

    def fetch(self, query: str) -> list[Job]:
        time.sleep(30)
        return []


class _RaisingCollector(Collector):
    query_based = True

    def __init__(self, name: str):
        super().__init__(get_json=lambda url, params: None)
        self.name = name

    def fetch(self, query: str) -> list[Job]:
        raise RuntimeError("kaboom")


class _BlockingCollector(Collector):
    query_based = True

    def __init__(self, name: str):
        super().__init__(get_json=lambda url, params: None)
        self.name = name
        self.started = threading.Event()
        self.release = threading.Event()

    def fetch(self, query: str) -> list[Job]:
        self.started.set()
        self.release.wait(10)
        return []


def _wait_until(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _approved_db(tmp_path):
    db = tmp_path / "dash.db"
    conn = connect(db)
    init_db(conn)
    save_profile(
        conn,
        CandidateProfile(
            approved=True, locations=["Remote"], target_roles=["Backend"], experience_level="mid"
        ),
    )
    conn.close()
    return db


def test_find_run_records_timeout_and_error_and_keeps_going(tmp_path, monkeypatch):
    db = _approved_db(tmp_path)
    monkeypatch.setattr(
        sourcing,
        "build_collectors",
        lambda settings, **kw: [_HangingCollector("hang"), _RaisingCollector("boom")],
    )
    monkeypatch.setattr(sourcing, "SOURCE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(sourcing, "COLLECTION_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cli, "_make_client", lambda: make_client())

    assert dashboard.start_pipeline(db) is True
    assert _wait_until(lambda: not dashboard._pipeline_status["running"])

    errors = dashboard._pipeline_status["errors"]
    assert "timed out" in errors["hang"]
    assert "kaboom" in errors["boom"]
    assert [s["source"] for s in dashboard._pipeline_status["sources"]] == ["hang", "boom"]

    # The lock/flag were released in the finally block: a new run can start.
    assert dashboard._pipeline_lock.acquire(timeout=1)
    dashboard._pipeline_lock.release()
    monkeypatch.setattr(sourcing, "build_collectors", lambda settings, **kw: [])
    assert dashboard.start_pipeline(db) is True
    assert _wait_until(lambda: not dashboard._pipeline_status["running"])


def test_status_shows_the_current_source_mid_run(tmp_path, monkeypatch):
    db = _approved_db(tmp_path)
    blocker = _BlockingCollector("blocker")
    monkeypatch.setattr(sourcing, "build_collectors", lambda settings, **kw: [blocker])
    monkeypatch.setattr(sourcing, "SOURCE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(cli, "_make_client", lambda: make_client())

    assert dashboard.start_pipeline(db) is True
    assert blocker.started.wait(5)
    assert _wait_until(lambda: dashboard._pipeline_status["current_source"] == "blocker")
    assert dashboard._pipeline_status["stage"] == "collecting"
    assert dashboard._pipeline_status["running"] is True

    blocker.release.set()
    assert _wait_until(lambda: not dashboard._pipeline_status["running"])
    assert dashboard._pipeline_status["current_source"] is None
    assert "blocker" in [s["source"] for s in dashboard._pipeline_status["sources"]]


def test_status_page_shows_current_source_and_elapsed(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    dashboard._pipeline_status["running"] = True
    dashboard._pipeline_status["stage"] = "collecting"
    dashboard._pipeline_status["current_source"] = "himalayas"
    dashboard._pipeline_status["started_at"] = time.time() - 5

    client = _logged_in(monkeypatch, tmp_path)
    response = client.get("/status")
    assert response.status_code == 200
    assert "Current source:" in response.text
    assert "himalayas" in response.text
    assert "Elapsed:" in response.text
