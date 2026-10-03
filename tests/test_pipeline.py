import json

from fake_llm import make_client

from atlas.config import get_settings
from atlas.db import connect, init_db, save_profile, start_run, upsert_job
from atlas.pipeline import process_job
from atlas.schemas import CandidateProfile, Job, Proficiency, Recommendation, Skill

PARSED = {
    "title": "Junior Python Developer",
    "title_seniority": "junior",
    "min_years_experience": 0,
    "max_years_experience": 2,
    "must_have_skills": ["Python"],
    "nice_to_have_skills": [],
    "remote_type": "remote",
    "confidence": 0.9,
}

EVAL = {
    "fit_score": 85,
    "skills_fit": 90,
    "seniority_fit": 95,
    "role_fit": 80,
    "growth_fit": 75,
    "company_signal": 60,
    "recommendation": "apply",
    "reasons_for": [{"quote": "0-2 years experience", "source": "jd"}],
    "reasons_against": [],
}

VERIFY_OK = {"veto": False, "downgrade_to": None, "reasons_against": []}
VERIFY_VETO = {
    "veto": True,
    "downgrade_to": "skip",
    "reasons_against": [{"quote": "0-2 years experience", "source": "jd"}],
}


def _setup(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    profile = CandidateProfile(
        full_name="A",
        email="a@b.c",
        target_roles=["Backend"],
        total_experience_months=6,
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
    )
    save_profile(conn, profile)
    run_id = start_run(conn, "run")
    job = Job(
        source="t",
        url="http://x",
        dedupe_key="k",
        title="Junior Python Developer",
        company="C",
        location="Remote",
        remote_type="remote",
        description_raw="We are hiring a Junior Python Developer. 0-2 years experience. Fully remote.",
    )
    job_id, _ = upsert_job(conn, run_id, job)
    return conn, run_id, profile, job_id, job


def test_process_job_happy_path(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)

    result = process_job(client, job_id, job, profile, get_settings(), conn=conn)

    assert result.filter_result.passed
    assert result.verdict is not None
    assert result.final_recommendation == Recommendation.apply
    assert result.score > 0
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1


def test_process_job_verifier_veto_skips(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_VETO)

    result = process_job(client, job_id, job, profile, get_settings(), conn=conn)

    assert result.final_recommendation == Recommendation.skip


def test_process_job_filter_rejection_short_circuits(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)
    job.title = "Senior Python Developer"
    senior_parsed = {**PARSED, "title_seniority": "senior"}
    client = make_client(parsed=senior_parsed, verdict=EVAL, verifier=VERIFY_OK)

    result = process_job(client, job_id, job, profile, get_settings(), conn=conn)

    assert result.filter_result.passed is False
    assert result.verdict is None
    assert result.final_recommendation == Recommendation.skip
    rejection_rules = json.dumps([r.rule_id for r in result.filter_result.rejections])
    assert "senior_title" in rejection_rules
