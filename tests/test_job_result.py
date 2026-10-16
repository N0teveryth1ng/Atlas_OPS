"""Per-job result page: score, dimensions, vetoes, deduped reasons, owner verdicts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from atlas.dashboard import create_app
from atlas.db import (
    connect,
    init_db,
    latest_owner_verdict,
    log_stage,
    owner_verdict_history,
    save_owner_verdict,
    start_run,
    upsert_job,
)
from atlas.schemas import Job


def _connect(tmp_path):
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    return conn


def _add_job(conn, run_id, *, url, title="Backend Engineer", passed: bool = True, source="test"):
    job_id, _ = upsert_job(conn, run_id, Job(source=source, url=url, dedupe_key=url, title=title))
    log_stage(conn, "filter_results", job_id=job_id, passed=passed)
    return job_id


def _full_decision(outcome: str = "review") -> dict:
    return {
        "outcome": outcome,
        "match_score": 55,
        "confidence": 0.62,
        "dimensions": {"skills": 70.0, "seniority": 40.0, "role": 60.0},
        "weights": {"skills": 0.5, "seniority": 0.3, "role": 0.2},
        "spreads": {"skills": 4.0, "seniority": 10.0, "role": 0.0},
        "risk_penalty": 3.0,
        "vetoes": [
            {
                "rule_id": "R_SENIORITY",
                "reason": "Role expects 8+ years",
                "hard": True,
                "evidence": {"quote": "minimum 8 years", "source": "description"},
            }
        ],
        "reasons_for": [
            {"quote": "Python and SQL", "source": "description"},
            {"quote": "Shipped data pipelines", "source": "resume"},
        ],
        "reasons_against": [
            {"quote": "Python and SQL", "source": "description"},
            {"quote": "10+ years required", "source": "description"},
        ],
        "missing_skills": ["Kubernetes", "Terraform"],
    }


def _decide(conn, job_id, decision: dict, *, seniority_quote: str = "Mid-level role"):
    log_stage(conn, "parsed_jds", job_id=job_id, data={"title_seniority": "unknown"})
    log_stage(
        conn,
        "evaluations",
        job_id=job_id,
        data={
            "verdict": {"seniority_assessment": {"quote": seniority_quote, "source": "description"}}
        },
    )
    log_stage(conn, "decisions", job_id=job_id, data=decision)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "dash.db"))


def test_job_result_page_shows_score_breakdown_confidence_vetoes(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision())
    conn.close()

    response = _client(tmp_path).get(f"/jobs/{job_id}")
    assert response.status_code == 200
    text = response.text
    assert "55 / 100 (REVIEW starts at 65)" in text
    assert "62%" in text
    assert "Mid-level role" in text
    assert "Kubernetes" in text
    assert "Terraform" in text
    assert "skills" in text and "70.0" in text
    assert "seniority" in text and "40.0" in text
    assert "R_SENIORITY" in text
    assert "Role expects 8+ years" in text
    assert "minimum 8 years" in text


def test_job_result_page_dedupes_reasons_and_marks_both_sides(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision())
    conn.close()

    text = _client(tmp_path).get(f"/jobs/{job_id}").text
    assert "Cited on both sides" in text
    assert text.count("Python and SQL") == 1
    # Un-explained reasons fall under "Evidence", not the for/against lists.
    assert "Evidence" in text
    assert "Shipped data pipelines" in text
    assert "10+ years required" in text


def test_job_result_page_shows_prepare_link_and_verdict_form(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision(outcome="skip"))
    conn.close()

    text = _client(tmp_path).get(f"/jobs/{job_id}").text
    assert f'href="/jobs/{job_id}/kit"' in text
    assert "Prepare application" in text
    assert "Your verdict" in text
    assert f'action="/jobs/{job_id}/verdict"' in text
    # Prepare application works for any decision, including SKIP.
    assert _client(tmp_path).get(f"/jobs/{job_id}/kit").status_code == 200


def test_results_row_links_to_job_page(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision(outcome="apply"))
    conn.close()

    text = _client(tmp_path).get("/").text
    assert f'href="/jobs/{job_id}"' in text


def test_job_result_page_404_for_failed_or_unknown(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    failed_id = _add_job(conn, run_id, url="http://x/failed", passed=False)
    conn.close()

    client = _client(tmp_path)
    assert client.get(f"/jobs/{failed_id}").status_code == 404
    assert client.get("/jobs/9999").status_code == 404


def test_post_verdict_stores_latest_and_redirects(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision(outcome="review"))
    conn.close()

    client = _client(tmp_path)
    response = client.post(
        f"/jobs/{job_id}/verdict",
        data={"verdict": "apply", "reason": "worth a punt"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{job_id}"

    text = client.get(f"/jobs/{job_id}").text
    assert "APPLY" in text
    assert "worth a punt" in text

    conn = connect(tmp_path / "dash.db")
    latest = latest_owner_verdict(conn, job_id)
    conn.close()
    assert latest is not None
    assert latest["verdict"] == "apply"
    assert latest["reason"] == "worth a punt"
    assert latest["system_decision"] == "review"


def test_post_verdict_appends_history_and_shows_latest(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision(outcome="review"))
    conn.close()

    client = _client(tmp_path)
    client.post(f"/jobs/{job_id}/verdict", data={"verdict": "skip", "reason": "too senior"})
    client.post(f"/jobs/{job_id}/verdict", data={"verdict": "apply", "reason": "changed mind"})

    conn = connect(tmp_path / "dash.db")
    history = owner_verdict_history(conn, job_id)
    conn.close()
    assert [entry["verdict"] for entry in history] == ["skip", "apply"]

    text = client.get(f"/jobs/{job_id}").text
    assert "changed mind" in text
    assert "too senior" not in text


def test_post_verdict_rejects_unknown_choice(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision())
    conn.close()

    response = _client(tmp_path).post(
        f"/jobs/{job_id}/verdict", data={"verdict": "maybe"}, follow_redirects=False
    )
    assert response.status_code == 400


def test_post_verdict_rejects_cross_origin(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision())
    conn.close()

    response = _client(tmp_path).post(
        f"/jobs/{job_id}/verdict",
        data={"verdict": "apply"},
        headers={"origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_owner_verdict_never_writes_eval_labels(tmp_path):
    labels = Path("eval/labels.jsonl")
    before = labels.read_bytes() if labels.exists() else None

    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    _decide(conn, job_id, _full_decision())
    conn.close()

    _client(tmp_path).post(f"/jobs/{job_id}/verdict", data={"verdict": "apply", "reason": "ok"})

    after = labels.read_bytes() if labels.exists() else None
    assert after == before


def test_save_owner_verdict_validates_choice(tmp_path):
    conn = _connect(tmp_path)
    run_id = start_run(conn, "test")
    job_id = _add_job(conn, run_id, url="http://x/1")
    conn.close()

    conn = connect(tmp_path / "dash.db")
    try:
        save_owner_verdict(conn, job_id, "apply", "yes", "review")
    except ValueError:  # pragma: no cover - guard keeps the happy path honest
        raise AssertionError("valid verdict should not raise") from None
    assert latest_owner_verdict(conn, job_id)["verdict"] == "apply"
    try:
        save_owner_verdict(conn, job_id, "nope")
    except ValueError as exc:
        assert "unknown owner verdict" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unknown verdict should raise ValueError")
    conn.close()
