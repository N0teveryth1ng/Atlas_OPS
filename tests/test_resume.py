"""Tailored-resume generator tests (fake LLM, no network).

Every summary sentence and bullet must carry a verbatim profile quote; a line
that invents a skill, a metric or a technology, or repeats a job keyword, is
dropped. Job requirements the profile lacks are listed as missing and never
added. The layout must pass the ATS lint. Structural fields (dates, employers,
education) are copied verbatim from the profile.
"""

from __future__ import annotations

import json

import pytest
from fake_llm import FakeClient

from atlas.config import get_settings
from atlas.db import (
    add_llm_tokens,
    approve_tailored_resume,
    approved_tailored_resume,
    connect,
    get_tailored_resume,
    init_db,
    latest_tailored_resume,
    save_tailored_resume,
    start_run,
    tokens_used_today,
    upsert_job,
)
from atlas.llm import LLMClient
from atlas.pipeline import LLMCallBudgetExceeded
from atlas.resume import (
    ResumeBlock,
    ats_lint,
    generate_tailored_resume,
    generate_tailored_resume_under_caps,
    resume_blocks,
)
from atlas.schemas import (
    CandidateProfile,
    Education,
    Experience,
    InputMode,
    Job,
    Links,
    ParsedJD,
    Project,
    Skill,
    SkillCategory,
)

VALID_SUMMARY = {
    "text": "Built a live job pipeline.",
    "quote": "Builds a job pipeline",
    "source": "profile",
}
VALID_EXPERIENCE_BULLET = {
    "text": "Built a job pipeline in Python.",
    "quote": "Builds a job pipeline",
    "source": "profile",
}
VALID_PROJECT_BULLET = {
    "text": "Shipped a live job pipeline.",
    "quote": "Builds a job pipeline",
    "source": "profile",
}


def _client(payload: dict) -> LLMClient:
    """A fake Groq-like client that always returns ``payload``; no network."""
    return LLMClient(client=FakeClient(lambda kwargs: json.dumps(payload)))


def _profile() -> CandidateProfile:
    return CandidateProfile(
        source_mode=InputMode.resume,
        full_name="Ada Lovelace",
        email="ada@example.com",
        links=Links(github="https://github.com/ada"),
        total_experience_months=30,
        experience_level="mid",
        education=[
            Education(
                degree="BSc",
                field="Computer Science",
                institution="University of X",
                graduation_year=2020,
            )
        ],
        certifications=["AWS Certified"],
        skills=[
            Skill(name="Python", canonical_name="python", category=SkillCategory.language),
            Skill(name="React", canonical_name="react", category=SkillCategory.framework),
            Skill(name="Docker", canonical_name="docker", category=SkillCategory.tool),
            Skill(
                name="PostgreSQL",
                canonical_name="postgresql",
                category=SkillCategory.database,
            ),
            Skill(name="FastAPI", canonical_name="fastapi", category=SkillCategory.framework),
        ],
        projects=[
            Project(
                title="Atlas Ops",
                tech=["Python", "FastAPI"],
                summary="Builds a job pipeline",
                outcomes="live",
            )
        ],
        work=[
            Experience(role="Data engineer", company="Acme", duration_months=30, tech=["Python"])
        ],
        target_roles=["Backend"],
    )


def _job() -> Job:
    return Job(
        source="test",
        url="https://acme.com/jobs/1",
        dedupe_key="acme-1",
        title="Backend engineer",
        company="Acme",
        location="Remote",
        description_raw="We are hiring a Backend engineer who knows Python and ReactJS.",
    )


def _parsed_jd() -> ParsedJD:
    return ParsedJD(
        title="Backend engineer",
        must_have_skills=["Python", "Kubernetes"],
        nice_to_have_skills=["ReactJS"],
        confidence=0.9,
    )


def _payload(**overrides) -> dict:
    base = {
        "summary": [VALID_SUMMARY],
        "skills": ["Python", "React", "Docker"],
        "experience": [{"bullets": [VALID_EXPERIENCE_BULLET]}],
        "projects": [{"bullets": [VALID_PROJECT_BULLET]}],
    }
    base.update(overrides)
    return base


def _generate(payload: dict):
    return generate_tailored_resume(
        _client(payload), profile=_profile(), job=_job(), parsed_jd=_parsed_jd()
    )


def test_generate_keeps_valid_grounded_lines_and_records_prompt_version():
    resume = _generate(_payload())

    assert [line.text for line in resume.summary] == [VALID_SUMMARY["text"]]
    assert resume.skills == ["Python", "React", "Docker"]
    assert [b.text for b in resume.experience[0].bullets] == [VALID_EXPERIENCE_BULLET["text"]]
    assert [b.text for b in resume.projects[0].bullets] == [VALID_PROJECT_BULLET["text"]]
    assert resume.prompt_version == "1"
    assert resume.model
    assert resume.ats.passed is True


def test_fabricated_skill_is_dropped():
    resume = _generate(_payload(skills=["Python", "Kubernetes", "React"]))

    assert resume.skills == ["Python", "React"]
    assert "Kubernetes" not in resume.skills


def test_fabricated_metric_is_dropped():
    bullet = {
        "text": "Built a job pipeline handling 5 million rows.",
        "quote": "Builds a job pipeline",
        "source": "profile",
    }
    resume = _generate(_payload(experience=[{"bullets": [bullet]}]))

    assert resume.experience[0].bullets == []
    assert "5 million" not in " ".join(line.text for line in resume.summary)


def test_bullet_without_a_valid_profile_quote_is_dropped():
    bullet = {
        "text": "Has a PhD in machine learning.",
        "quote": "PhD in machine learning",
        "source": "profile",
    }
    resume = _generate(_payload(projects=[{"bullets": [bullet]}]))

    assert resume.projects[0].bullets == []


def test_job_keyword_is_not_repeated_across_bullets():
    repeat = {
        "text": "Wrote Python tools.",
        "quote": "Builds a job pipeline",
        "source": "profile",
    }
    resume = _generate(_payload(experience=[{"bullets": [VALID_EXPERIENCE_BULLET, repeat]}]))

    texts = [b.text for b in resume.experience[0].bullets]
    assert texts == [VALID_EXPERIENCE_BULLET["text"]]
    assert "Wrote Python tools." not in texts


def test_missing_requirements_are_listed_and_never_added():
    resume = _generate(_payload(skills=["Python", "React", "Kubernetes"]))

    assert resume.missing_requirements == ["Kubernetes"]
    assert "Kubernetes" not in resume.skills
    all_text = " ".join(line.text for line in resume.summary) + " ".join(
        b.text for entry in (*resume.experience, *resume.projects) for b in entry.bullets
    )
    assert "Kubernetes" not in all_text


def test_reactjs_job_keyword_matches_the_profile_react_skill():
    """The job's "ReactJS" wording is the profile's existing "React" skill."""
    resume = _generate(_payload(skills=["Python", "ReactJS"]))

    assert "Kubernetes" in resume.missing_requirements
    assert resume.missing_requirements == ["Kubernetes"]
    assert resume.skills == ["Python", "ReactJS"]


def test_ats_lint_passes_a_clean_layout_and_flags_a_table():
    resume = _generate(_payload())
    clean = ats_lint(resume_blocks(resume))
    assert clean.passed is True
    assert clean.reasons == []

    with_table = ats_lint([ResumeBlock(kind="table", text="Skills"), *resume_blocks(resume)])
    assert with_table.passed is False
    assert any("table" in reason for reason in with_table.reasons)


def test_ats_lint_flags_hidden_text_and_missing_heading():
    blocks = [ResumeBlock(kind="paragraph", text="hi\u200b"), ResumeBlock(kind="heading", text="X")]
    report = ats_lint(blocks)

    assert report.passed is False
    assert any("hidden" in reason for reason in report.reasons)
    assert any("missing standard heading" in reason for reason in report.reasons)


def test_dates_and_employers_are_preserved_from_the_profile():
    resume = _generate(_payload())

    entry = resume.experience[0]
    assert entry.role == "Data engineer"
    assert entry.company == "Acme"
    assert entry.duration_months == 30
    assert resume.projects[0].title == "Atlas Ops"
    assert resume.projects[0].tech == ["Python", "FastAPI"]
    assert resume.education[0]["institution"] == "University of X"
    assert resume.certifications == ["AWS Certified"]


def test_profile_and_job_text_never_reach_braintrust():
    from test_observability import install_sdk

    from atlas.observability import reset_observability

    profile_marker = "zzprofilemarker9f3a"
    jd_marker = "zzjdmarker9f3a"
    profile = _profile()
    profile.skills.append(
        Skill(name=profile_marker, canonical_name=profile_marker, category=SkillCategory.other)
    )
    job = _job()
    job.description_raw = f"{job.description_raw} {jd_marker}"
    client = _client(_payload())

    sdk = install_sdk()
    try:
        generate_tailored_resume(client, profile=profile, job=job, parsed_jd=_parsed_jd())
    finally:
        reset_observability()

    assert sdk.spans, "expected at least one Braintrust span"
    blob = "".join(span.sent() for span in sdk.spans).lower()
    assert profile_marker not in blob
    assert jd_marker not in blob


def test_generation_under_caps_records_tokens_to_the_daily_budget(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)

    class _CountingClient:
        def __init__(self, inner: LLMClient, per_call: int) -> None:
            self._inner = inner
            self.per_call = per_call
            self.tokens_used = 0

        def call_json(self, **kwargs):
            self.tokens_used += self.per_call
            return self._inner.call_json(**kwargs)

    client = _CountingClient(_client(_payload()), per_call=100)
    before = tokens_used_today(conn)

    generate_tailored_resume_under_caps(
        conn, client, profile=_profile(), job=_job(), parsed_jd=_parsed_jd()
    )

    assert tokens_used_today(conn) == before + 100


def test_generation_under_caps_enforces_the_per_run_call_cap(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)
    settings = get_settings().model_copy(deep=True)
    settings.run.max_llm_calls_per_run = 0

    with pytest.raises(LLMCallBudgetExceeded):
        generate_tailored_resume_under_caps(
            conn,
            _client(_payload()),
            profile=_profile(),
            job=_job(),
            parsed_jd=_parsed_jd(),
            settings=settings,
        )


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def _conn_with_job(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)
    run_id = start_run(conn, "test")
    job_id, _created = upsert_job(conn, run_id, _job())
    return conn, job_id


def test_save_tailored_resume_increments_version_per_job(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)

    first = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")
    second = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")

    assert first != second
    assert get_tailored_resume(conn, first)["version"] == 1
    assert get_tailored_resume(conn, second)["version"] == 2
    assert latest_tailored_resume(conn, job_id)["id"] == second
    assert get_tailored_resume(conn, first)["approved_at"] is None


def test_approve_sets_approved_at_and_marks_the_version_downloadable(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)
    resume_id = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")

    assert approved_tailored_resume(conn, job_id) is None
    approve_tailored_resume(conn, resume_id)

    row = get_tailored_resume(conn, resume_id)
    assert row["approved_at"] is not None
    assert approved_tailored_resume(conn, job_id)["id"] == resume_id


def test_approve_is_idempotent_and_keeps_the_first_timestamp(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)
    resume_id = save_tailored_resume(conn, job_id, content_json="{}", model="m", prompt_version="1")

    approve_tailored_resume(conn, resume_id)
    stamp = get_tailored_resume(conn, resume_id)["approved_at"]
    approve_tailored_resume(conn, resume_id)

    assert get_tailored_resume(conn, resume_id)["approved_at"] == stamp


def test_approve_unknown_resume_raises_key_error(tmp_path):
    conn, _job_id = _conn_with_job(tmp_path)

    with pytest.raises(KeyError):
        approve_tailored_resume(conn, 9999)


def test_latest_tailored_resume_is_none_without_a_build(tmp_path):
    conn, job_id = _conn_with_job(tmp_path)

    assert latest_tailored_resume(conn, job_id) is None


def test_add_llm_tokens_accumulates(tmp_path):
    conn = connect(tmp_path / "resume.db")
    init_db(conn)

    add_llm_tokens(conn, 40)
    add_llm_tokens(conn, 2)

    assert tokens_used_today(conn) == 42
