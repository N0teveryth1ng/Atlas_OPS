"""Step 2 (D-3): evidence-quote validation.

These tests fail before the change (fabricated quotes were accepted and jobs
shipped) and pass after (a job whose evidence cannot be verified is routed to
needs_review and never ranked/emailed).
"""

from __future__ import annotations

import json

import pytest

from atlas.config import get_settings
from atlas.db import connect, init_db, save_profile, start_run, upsert_job
from atlas.evidence import (
    EvidenceValidationError,
    validate_verdict_evidence,
    validate_verifier_evidence,
)
from atlas.evaluator import evaluate_job
from atlas.llm import LLMClient
from atlas.pipeline import process_job
from atlas.schemas import (
    CandidateProfile,
    Evidence,
    EvidenceSource,
    Job,
    ParsedJD,
    Proficiency,
    Recommendation,
    Seniority,
    Skill,
    Verdict,
    VerifierVerdict,
)
from atlas.skills import SkillMatch
from tests.fake_llm import FakeClient

JD = "We are hiring a Junior Python Developer. 0-2 years experience. Fully remote."
PROFILE_TEXT = "Candidate profile\nName: A\nSkills: python\n"


def _profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="A",
        email="a@b.c",
        target_roles=["Backend"],
        total_experience_months=6,
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
    )


def _parsed() -> ParsedJD:
    return ParsedJD(title="Junior Python Developer", title_seniority=Seniority.junior, min_years_experience=0)


def _match() -> SkillMatch:
    return SkillMatch(must_have_coverage=1.0, nice_to_have_coverage=0.5)


def _job() -> Job:
    return Job(
        source="t",
        url="http://x",
        dedupe_key="k",
        title="Junior Python Developer",
        company="C",
        location="Remote",
        remote_type="remote",
        description_raw=JD,
    )


# --------------------------------------------------------------------------- #
# Validator unit tests
# --------------------------------------------------------------------------- #


def test_verbatim_quote_accepted():
    verdict = Verdict(
        fit_score=80,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="Junior Python Developer", source=EvidenceSource.jd)],
        seniority_assessment=Evidence(quote="0-2 years experience", source=EvidenceSource.jd),
    )
    assert validate_verdict_evidence(verdict, JD, PROFILE_TEXT) == []


def test_whitespace_and_case_only_differences_accepted():
    verdict = Verdict(
        fit_score=80,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="0-2   YEARS experience", source=EvidenceSource.jd)],
    )
    assert validate_verdict_evidence(verdict, JD, PROFILE_TEXT) == []


def test_one_changed_word_rejected():
    verdict = Verdict(
        fit_score=80,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="0-3 years experience", source=EvidenceSource.jd)],
    )
    errors = validate_verdict_evidence(verdict, JD, PROFILE_TEXT)
    assert errors and "reasons_for[0]" in errors[0]


def test_empty_and_too_short_quotes_rejected():
    for quote in ("", "Python"):
        verdict = Verdict(
            fit_score=80,
            recommendation=Recommendation.apply,
            reasons_for=[Evidence(quote=quote, source=EvidenceSource.jd)],
        )
        assert validate_verdict_evidence(verdict, JD, PROFILE_TEXT)


def test_wrong_source_rejected():
    verdict = Verdict(
        fit_score=80,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="0-2 years experience", source=EvidenceSource.profile)],
    )
    assert validate_verdict_evidence(verdict, JD, PROFILE_TEXT)


def test_verifier_evidence_validation():
    good = VerifierVerdict(
        veto=True,
        reasons_against=[Evidence(quote="0-2 years experience", source=EvidenceSource.jd)],
    )
    bad = VerifierVerdict(
        veto=True,
        reasons_against=[Evidence(quote="must lead a team of ten", source=EvidenceSource.jd)],
    )
    assert validate_verifier_evidence(good, JD, PROFILE_TEXT) == []
    assert validate_verifier_evidence(bad, JD, PROFILE_TEXT)


def test_run_summary_counts_quote_failures():
    from atlas.digest import RunSummary

    summary = RunSummary()
    summary.quote_validation_failures = 2
    assert summary.quote_validation_failures == 2


# --------------------------------------------------------------------------- #
# Retry behaviour
# --------------------------------------------------------------------------- #


def _client(handler) -> LLMClient:
    return LLMClient(client=FakeClient(handler))


def test_fabricated_then_corrected_is_accepted():
    fabricated = {
        "fit_score": 80,
        "recommendation": "apply",
        "reasons_for": [{"quote": "candidate invents this claim", "source": "jd"}],
    }
    corrected = {
        "fit_score": 80,
        "recommendation": "apply",
        "reasons_for": [{"quote": "Junior Python Developer", "source": "jd"}],
    }
    calls = {"n": 0}

    def handler(_kwargs):
        calls["n"] += 1
        return json.dumps(fabricated if calls["n"] == 1 else corrected)

    verdict = evaluate_job(
        _client(handler), profile=_profile(), job=_job(), parsed_jd=_parsed(), skill_match=_match()
    )
    assert verdict.reasons_for[0].quote == "Junior Python Developer"
    assert calls["n"] == 2


def test_fabricated_quote_raises_after_retries():
    fabricated = {
        "fit_score": 80,
        "recommendation": "apply",
        "reasons_for": [{"quote": "candidate invents this claim", "source": "jd"}],
    }
    calls = {"n": 0}

    def handler(_kwargs):
        calls["n"] += 1
        return json.dumps(fabricated)

    with pytest.raises(EvidenceValidationError):
        evaluate_job(
            _client(handler), profile=_profile(), job=_job(), parsed_jd=_parsed(), skill_match=_match()
        )
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# Pipeline routing
# --------------------------------------------------------------------------- #

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
GOOD_VERDICT = {
    "fit_score": 85,
    "recommendation": "apply",
    "reasons_for": [{"quote": "Junior Python Developer", "source": "jd"}],
}


def _setup(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    profile = _profile()
    save_profile(conn, profile)
    run_id = start_run(conn, "run")
    job_id, _ = upsert_job(conn, run_id, _job())
    return conn, profile, job_id


def _routed(verdict_payload, verifier_payload):
    def handler(kwargs):
        system = kwargs["messages"][0]["content"]
        if "adversarial reviewer" in system:
            payload = verifier_payload
        elif "structured requirements" in system:
            payload = PARSED
        else:
            payload = verdict_payload
        return json.dumps(payload)

    return LLMClient(client=FakeClient(handler))


def test_unverifiable_evaluator_evidence_routes_to_needs_review(tmp_path):
    conn, profile, job_id = _setup(tmp_path)
    fabricated = {
        "fit_score": 85,
        "recommendation": "apply",
        "reasons_for": [{"quote": "totally made up requirement", "source": "jd"}],
    }
    result = process_job(
        _routed(fabricated, None), job_id, _job(), profile, get_settings(), conn=conn
    )
    conn.close()
    assert result.evidence_failed is True
    assert result.needs_review is True
    assert result.status.value == "needs_review"
    assert result.verdict is None


def test_unverifiable_verifier_veto_routes_to_needs_review(tmp_path):
    conn, profile, job_id = _setup(tmp_path)
    fabricated_veto = {
        "veto": True,
        "downgrade_to": "skip",
        "reasons_against": [{"quote": "must lead a team of ten", "source": "jd"}],
    }
    result = process_job(
        _routed(GOOD_VERDICT, fabricated_veto), job_id, _job(), profile, get_settings(), conn=conn
    )
    conn.close()
    assert result.evidence_failed is True
    assert result.needs_review is True
    assert result.final_recommendation != Recommendation.skip
