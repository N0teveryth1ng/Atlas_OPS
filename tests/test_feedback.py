import json

from atlas.config import get_settings
from atlas.db import (
    connect,
    init_db,
    record_feedback as db_record_feedback,
    start_run,
    upsert_job,
)
from atlas.feedback import apply_feedback, export_golden, record_feedback
from atlas.ranker import final_score
from atlas.schemas import Job, Recommendation, Verdict
from atlas.skills import SkillMatch


def _setup(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    run_id = start_run(conn, "t")
    job_id, _ = upsert_job(
        conn,
        run_id,
        Job(
            source="t",
            url="http://x",
            dedupe_key="k",
            title="Junior Python Dev",
            company="Acme",
            description_raw="Junior Python role, 0-2 years.",
        ),
    )
    return conn, job_id


def test_record_and_load_feedback(tmp_path):
    conn, job_id = _setup(tmp_path)
    feedback_id = record_feedback(conn, job_id, "bad", reason_code="too_senior", note="senior-ish")
    assert feedback_id > 0
    from atlas.db import load_feedback

    rows = load_feedback(conn)
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["reason_code"] == "too_senior"


def test_record_feedback_rejects_bad_input(tmp_path):
    import pytest

    conn, job_id = _setup(tmp_path)
    with pytest.raises(ValueError):
        record_feedback(conn, job_id, "maybe")
    with pytest.raises(ValueError):
        record_feedback(conn, job_id, "bad", reason_code="nonsense")


def test_apply_feedback_bumps_weights_and_blacklists():
    settings = get_settings()
    rows = [
        {"verdict": "bad", "reason_code": "wrong_stack", "company": "BadCo"},
        {"verdict": "bad", "reason_code": "wrong_stack", "company": "BadCo"},
        {"verdict": "bad", "reason_code": "bad_company", "company": "ScamCo"},
        {"verdict": "good", "reason_code": None, "company": "GoodCo"},
    ]
    tuned, tuning = apply_feedback(settings, rows)

    assert tuned.ranking.weights.must_have_coverage > settings.ranking.weights.must_have_coverage
    assert "ScamCo" in tuned.companies.blacklist_companies
    assert "GoodCo" in tuned.companies.target_companies
    weights_sum = sum(
        getattr(tuned.ranking.weights, key)
        for key in ("fit_score", "must_have_coverage", "seniority_fit", "preference_bonus")
    )
    assert abs(weights_sum - 1.0) < 1e-6
    assert tuning.feedback_count == 4


def test_feedback_demonstrably_changes_ranking():
    settings = get_settings()
    verdict = Verdict(fit_score=100, seniority_fit=100, recommendation=Recommendation.apply)
    match = SkillMatch(must_have_coverage=0.0, nice_to_have_coverage=0.0)
    job = Job(source="t", url="http://x", title="T", company="C")

    before = final_score(verdict, match, job, settings)

    rows = [{"verdict": "bad", "reason_code": "wrong_stack", "company": "C"}] * 3
    tuned, _ = apply_feedback(settings, rows)
    after = final_score(verdict, match, job, tuned)

    assert after < before


def test_export_golden_grows_eval_set(tmp_path):
    conn, job_id = _setup(tmp_path)
    db_record_feedback(conn, job_id, "good", None, None)
    out = tmp_path / "feedback.jsonl"

    assert export_golden(conn, out) == 1
    record = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert record["label"] == "apply"
    assert record["id"] == f"feedback_{job_id}"

    assert export_golden(conn, out) == 0
