"""Kit-route tests: generating, storing and rendering a kit (fake LLM).

No network: ``create_app`` gets an injected client factory returning a fake
Groq-like client. The page must show each claim/answer next to its supporting
quote and source, the copy buttons, the apply link and the mark-APLD button.
"""

from __future__ import annotations

import json

from fake_llm import FakeClient
from fastapi.testclient import TestClient

from atlas.dashboard import create_app
from atlas.db import (
    connect,
    init_db,
    latest_kit_data,
    log_stage,
    save_profile,
    start_run,
    upsert_job,
)
from atlas.llm import LLMClient
from atlas.schemas import CandidateProfile, Job, Links

KIT_PAYLOAD = {
    "summary": [
        {"text": "Acme needs a Data engineer.", "quote": "hiring a Data engineer", "source": "jd"},
        {
            "text": "The role builds pipelines in Python.",
            "quote": "build pipelines in Python",
            "source": "jd",
        },
        {
            "text": "Ada brings 30 months of experience.",
            "quote": "Experience: 30 months",
            "source": "profile",
        },
    ],
    "cover_note": [
        {
            "text": "I am a Data engineer who builds pipelines in Python.",
            "quote": "build pipelines in Python",
            "source": "jd",
        }
    ],
    "answers": [
        {
            "question_key": "why_company",
            "answer": "Acme is hiring a Data engineer.",
            "quote": "hiring a Data engineer",
            "source": "jd",
        },
        {
            "question_key": "links",
            "answer": "My GitHub is https://github.com/ada.",
            "quote": "github: https://github.com/ada",
            "source": "profile",
        },
    ],
}


def _fake_client() -> LLMClient:
    return LLMClient(client=FakeClient(lambda kwargs: json.dumps(KIT_PAYLOAD)))


def _job() -> Job:
    return Job(
        source="test",
        url="http://x/1",
        dedupe_key="k1",
        title="Data engineer",
        company="Acme",
        location="Remote",
        description_raw="We are hiring a Data engineer to build pipelines in Python.",
    )


def _profile() -> CandidateProfile:
    return CandidateProfile(
        approved=True,
        full_name="Ada Lovelace",
        links=Links(github="https://github.com/ada"),
        total_experience_months=30,
        experience_level="mid",
        target_roles=["Backend"],
        locations=["Remote"],
    )


def _setup(tmp_path, *, with_profile: bool = True, approved: bool = True) -> int:
    tmp_path.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_path / "dash.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(conn, run_id, _job())
    log_stage(conn, "filter_results", job_id=job_id, passed=True)
    log_stage(
        conn,
        "decisions",
        job_id=job_id,
        data={
            "outcome": "apply",
            "match_score": 80,
            "reasons_for": ["strong skill overlap"],
            "reasons_against": [],
            "missing_skills": [],
        },
    )
    if with_profile:
        save_profile(conn, _profile().model_copy(update={"approved": approved}))
    conn.close()
    return job_id


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "dash.db", client_factory=_fake_client))


def test_kit_post_generates_stores_and_renders_quotes(tmp_path):
    job_id = _setup(tmp_path)
    response = _client(tmp_path).post(f"/jobs/{job_id}/kit")

    assert response.status_code == 200
    text = response.text
    assert "Acme needs a Data engineer." in text
    assert "hiring a Data engineer" in text  # the supporting quote, shown
    assert "Experience: 30 months" in text
    assert "My GitHub is https://github.com/ada." in text  # an answer
    assert "profile" in text and "job" in text  # sources shown
    assert "Copy" in text  # copy buttons
    assert "Mark as APLD" in text  # mark-as-applied button
    assert 'href="http://x/1"' in text  # apply link

    conn = connect(tmp_path / "dash.db")
    data = latest_kit_data(conn, job_id)
    row = conn.execute(
        "SELECT text, model, prompt_version FROM kits WHERE job_id = ?", (job_id,)
    ).fetchone()
    conn.close()
    assert data is not None
    assert data["prompt_version"] == "3"
    assert data["model"]
    assert row is not None
    assert row["prompt_version"] == "3"
    assert "hiring a Data engineer" in row["text"]


def test_kit_get_shows_stored_kit(tmp_path):
    job_id = _setup(tmp_path)
    client = _client(tmp_path)

    before = client.get(f"/jobs/{job_id}/kit")
    assert before.status_code == 200
    assert "No kit generated yet." in before.text

    client.post(f"/jobs/{job_id}/kit")
    after = client.get(f"/jobs/{job_id}/kit")
    assert after.status_code == 200
    assert "Acme needs a Data engineer." in after.text
    assert "hiring a Data engineer" in after.text


def test_kit_post_requires_an_approved_profile(tmp_path):
    job_id = _setup(tmp_path, with_profile=False)
    response = _client(tmp_path).post(f"/jobs/{job_id}/kit")
    assert response.status_code == 400
    assert "approve a profile" in response.text.lower()

    job_id2 = _setup(tmp_path / "b", approved=False)
    response2 = TestClient(
        create_app(tmp_path / "b" / "dash.db", client_factory=_fake_client)
    ).post(f"/jobs/{job_id2}/kit")
    assert response2.status_code == 400
    assert "Approve your profile" in response2.text
