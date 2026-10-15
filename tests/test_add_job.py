"""Tests for the "Add a job" dashboard page (Task E).

A pasted job goes through the same pipeline stages a collected job does, but
synchronously, so these tests exercise the whole path: validation, dedupe, the
daily-limit guard, the run lock, the decision/rejection rendering, and the
Braintrust redaction guarantee.
"""

from __future__ import annotations

from fake_llm import FakeClient, make_client
from fastapi.testclient import TestClient

from atlas.config import get_settings
from atlas.dashboard import create_app
from atlas.db import add_llm_tokens, connect, init_db, save_profile
from atlas.llm import LLMClient
from atlas.schemas import CandidateProfile, Proficiency, Skill

_JUNIOR_JD = "We are hiring a Junior Python Developer. 0-2 years experience. Fully remote."

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


def _db(tmp_path, *, skills=None):
    db = tmp_path / "add.db"
    conn = connect(db)
    init_db(conn)
    profile = CandidateProfile(
        full_name="A",
        email="a@b.c",
        target_roles=["Backend"],
        total_experience_months=6,
        skills=skills
        or [Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
    )
    profile.approved = True
    save_profile(conn, profile)
    return conn, db


def _app(db, client):
    return create_app(db, client_factory=lambda: client)


def _recording_client(handler):
    fake = FakeClient(handler)
    return LLMClient(client=fake), fake


def _boom(_kwargs):
    raise AssertionError("the LLM must not be called for this job")


def _form(**overrides):
    data = {
        "title": "Junior Python Developer",
        "company": "Acme",
        "location": "Remote",
        "apply_url": "https://acme.example/jobs/1",
        "description": _JUNIOR_JD,
    }
    data.update(overrides)
    return data


def test_senior_job_rejected_with_zero_llm_calls(tmp_path):
    conn, db = _db(tmp_path)
    conn.close()
    client, fake = _recording_client(_boom)

    response = TestClient(_app(db, client)).post(
        "/add",
        data=_form(title="Senior Python Developer", description="Senior role, 8+ years."),
    )

    assert response.status_code == 200
    assert "Rejected" in response.text
    assert "senior_title" in response.text
    assert len(fake.chat.completions.calls) == 0


def test_junior_job_reaches_a_decision(tmp_path):
    conn, db = _db(tmp_path)
    conn.close()
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)

    response = TestClient(_app(db, client)).post("/add", data=_form())

    assert response.status_code == 200
    assert "Decision: APPLY" in response.text
    assert "Match score" in response.text
    assert "Reasons for" in response.text


def test_duplicate_paste_is_deduped(tmp_path):
    conn, db = _db(tmp_path)
    conn.close()
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)
    test_client = TestClient(_app(db, client))

    first = test_client.post("/add", data=_form())
    assert "Decision: APPLY" in first.text

    second = test_client.post("/add", data=_form(description="A different paste entirely."))
    assert "already in your list" in second.text

    conn = connect(db)
    count = conn.execute("SELECT COUNT(*) FROM jobs WHERE source = 'owner_added'").fetchone()[0]
    conn.close()
    assert count == 1


def test_add_requires_login(tmp_path, monkeypatch):
    conn, db = _db(tmp_path)
    conn.close()
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)
    test_client = TestClient(_app(db, client))

    get_response = test_client.get("/add", follow_redirects=False)
    assert get_response.status_code == 303
    assert get_response.headers["location"] == "/login"

    post_response = test_client.post("/add", data=_form(), follow_redirects=False)
    assert post_response.status_code == 303
    assert post_response.headers["location"] == "/login"


def test_cross_origin_post_is_rejected(tmp_path):
    conn, db = _db(tmp_path)
    conn.close()
    client, fake = _recording_client(_boom)

    response = TestClient(_app(db, client)).post(
        "/add", data=_form(), headers={"origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert len(fake.chat.completions.calls) == 0


def test_oversized_description_is_rejected(tmp_path):
    conn, db = _db(tmp_path)
    conn.close()
    client, fake = _recording_client(_boom)

    response = TestClient(_app(db, client)).post("/add", data=_form(description="a" * 20001))

    assert response.status_code == 422
    assert "too long" in response.text
    assert len(fake.chat.completions.calls) == 0


def test_daily_limit_guard_blocks_add(tmp_path):
    conn, db = _db(tmp_path)
    add_llm_tokens(conn, get_settings().run.max_llm_tokens_per_day)
    conn.close()
    client, fake = _recording_client(_boom)

    response = TestClient(_app(db, client)).post("/add", data=_form())

    assert response.status_code == 200
    assert "Groq daily limit reached, resets in about" in response.text
    assert len(fake.chat.completions.calls) == 0


def test_add_refused_while_a_run_is_in_progress(tmp_path):
    conn, db = _db(tmp_path)
    conn.close()
    client, fake = _recording_client(_boom)

    from atlas import dashboard as dash

    saved = dict(dash._pipeline_status)
    dash._pipeline_status["running"] = True
    try:
        response = TestClient(_app(db, client)).post("/add", data=_form())
    finally:
        dash._pipeline_status.clear()
        dash._pipeline_status.update(saved)

    assert response.status_code == 200
    assert "already in progress" in response.text
    assert len(fake.chat.completions.calls) == 0


def test_jd_and_profile_text_never_reach_braintrust(tmp_path):
    from test_observability import install_sdk

    from atlas.observability import reset_observability

    jd_marker = "zzjdmarker9f3a"
    profile_marker = "zzprofilemarker9f3a"
    conn, db = _db(
        tmp_path,
        skills=[
            Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong),
            Skill(
                name=profile_marker,
                canonical_name=profile_marker,
                proficiency=Proficiency.strong,
            ),
        ],
    )
    conn.close()
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)

    sdk = install_sdk()
    try:
        response = TestClient(_app(db, client)).post(
            "/add", data=_form(description=f"{_JUNIOR_JD} {jd_marker}")
        )
    finally:
        reset_observability()

    assert response.status_code == 200
    assert sdk.spans, "expected at least one Braintrust span"
    blob = "".join(span.sent() for span in sdk.spans).lower()
    assert jd_marker not in blob
    assert profile_marker not in blob
