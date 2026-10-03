import json

from atlas.config import get_settings
from atlas.db import (
    connect,
    init_db,
    record_feedback as db_record_feedback,
    start_run,
    upsert_job,
)
from atlas.feedback import (
    FeedbackTuning,
    _renormalise,
    apply_feedback,
    export_golden,
    load_and_apply,
    record_feedback,
)
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


def test_summary_variants():
    assert FeedbackTuning().summary() == "No feedback recorded yet."
    tuning = FeedbackTuning(feedback_count=3, blacklist_added=["BadCo"], targets_added=["GoodCo"])
    text = tuning.summary()
    assert "3 feedback item(s) applied" in text
    assert "blacklisted: BadCo" in text
    assert "boosted: GoodCo" in text


def test_apply_feedback_empty_and_renormalise_guard():
    settings = get_settings()
    tuned, tuning = apply_feedback(settings, [])
    assert tuning.feedback_count == 0
    assert tuned.ranking.weights.fit_score == settings.ranking.weights.fit_score
    assert _renormalise({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}


def test_too_senior_bumps_seniority_weight():
    settings = get_settings()
    rows = [{"verdict": "bad", "reason_code": "too_senior", "company": "X"}]
    tuned, _ = apply_feedback(settings, rows)
    assert tuned.ranking.weights.seniority_fit > settings.ranking.weights.seniority_fit


def test_load_and_apply_uses_stored_feedback(tmp_path):
    conn, job_id = _setup(tmp_path)
    db_record_feedback(conn, job_id, "bad", "bad_company", None)
    settings = get_settings()
    tuned, tuning = load_and_apply(conn, settings)
    conn.close()
    assert tuning.feedback_count == 1
    assert "Acme" in tuned.companies.blacklist_companies


def test_export_golden_skips_incomplete_and_duplicate(tmp_path):
    conn, job_id = _setup(tmp_path)
    run_id = start_run(conn, "t2")
    upsert_job(conn, run_id, Job(source="t", url="http://y", dedupe_key="k2", title="NoDesc"))
    db_record_feedback(conn, job_id, "bad", "other", None)
    db_record_feedback(conn, 2, "good", None, None)
    out = tmp_path / "feedback.jsonl"
    out.write_text("not json\n" + json.dumps({"id": f"feedback_{job_id}"}) + "\n", encoding="utf-8")

    written = export_golden(conn, out)
    conn.close()
    assert written == 0


def test_export_golden_grows_eval_set(tmp_path):
    conn, job_id = _setup(tmp_path)
    db_record_feedback(conn, job_id, "good", None, None)
    out = tmp_path / "feedback.jsonl"

    assert export_golden(conn, out) == 1
    record = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert record["label"] == "apply"
    assert record["id"] == f"feedback_{job_id}"

    assert export_golden(conn, out) == 0
