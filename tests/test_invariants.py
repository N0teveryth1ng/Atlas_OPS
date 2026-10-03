"""Step 1 (D-4) invariants: a filtered or unverified job can never ship.

These tests fail before the invariant was introduced (the ranker/digest/emailer
happily accepted any job) and pass after.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlas.config import get_settings
from atlas.db import connect, get_job_status, init_db, save_profile, start_run, upsert_job
from atlas.digest import Digest, DigestItem, DigestSection, build_digest
from atlas.emailer import ResendEmailer, send_digest
from atlas.job_status import (
    IllegalTransition,
    InvariantViolation,
    JobStatus,
    assert_sent_subset,
    transition,
)
from atlas.pipeline import ProcessedJob, run_pipeline
from atlas.ranker import rank
from atlas.schemas import (
    CandidateProfile,
    Evidence,
    EvidenceSource,
    ExperienceLevel,
    FilterRejection,
    FilterResult,
    Job,
    Recommendation,
    Verdict,
    VerifierVerdict,
)
from atlas.skills import SkillMatch
from tests.fake_llm import make_client


def _job(i: int, title: str = "Junior Python Dev") -> Job:
    return Job(source="t", url=f"http://x/{i}", dedupe_key=f"k{i}", title=title, company="C")


def _verdict(score: float = 90.0) -> Verdict:
    return Verdict(
        fit_score=score,
        recommendation=Recommendation.strong_apply,
        reasons_for=[Evidence(quote="Junior Python Dev", source=EvidenceSource.jd)],
    )


def _passed_result(i: int, score: float = 85.0, status: JobStatus = JobStatus.ranked) -> ProcessedJob:
    return ProcessedJob(
        job_id=i,
        job=_job(i),
        filter_result=FilterResult(passed=True),
        skill_match=SkillMatch(must_have_coverage=1.0, nice_to_have_coverage=1.0),
        verdict=_verdict(score),
        verifier=VerifierVerdict(),
        final_recommendation=Recommendation.strong_apply,
        score=score,
        status=status,
    )


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


def test_illegal_transition_is_rejected():
    with pytest.raises(IllegalTransition):
        transition(JobStatus.rejected, JobStatus.evaluated)
    with pytest.raises(IllegalTransition):
        transition(JobStatus.new, JobStatus.evaluated)


def test_legal_transition_is_returned():
    assert transition(JobStatus.new, JobStatus.parsed) == JobStatus.parsed


def test_sent_subset_invariant():
    assert_sent_subset({1, 2, 3}, {1, 2})  # ok
    with pytest.raises(InvariantViolation):
        assert_sent_subset({1, 2}, {1, 3})


# --------------------------------------------------------------------------- #
# Ranker enforcement (bypassing the pipeline must not bypass the rule)
# --------------------------------------------------------------------------- #


def test_rank_raises_on_rejected_job():
    result = _passed_result(1)
    result.filter_result = FilterResult(
        passed=False, rejections=[FilterRejection(rule_id="senior_title", evidence="senior")]
    )
    result.status = JobStatus.rejected
    with pytest.raises(InvariantViolation):
        rank([result])


def test_rank_raises_when_filter_result_missing():
    result = _passed_result(1, status=JobStatus.verified)
    result.filter_result = None
    with pytest.raises(InvariantViolation):
        rank([result])


def test_rank_returns_verified_passed_jobs():
    ranked = rank([_passed_result(1, 70), _passed_result(2, 90)])
    assert [r.job_id for r in ranked] == [2, 1]


# --------------------------------------------------------------------------- #
# Digest enforcement
# --------------------------------------------------------------------------- #


def test_digest_raises_on_needs_review_job():
    result = _passed_result(1)
    result.status = JobStatus.needs_review
    result.needs_review = True
    with pytest.raises(InvariantViolation):
        build_digest([result], get_settings())


def test_missing_filter_result_is_treated_as_not_passed():
    result = _passed_result(1)
    result.filter_result = None
    digest = build_digest([result], get_settings())
    assert digest.is_empty()
    assert digest.summary.filtered_out == 1


def test_digest_raises_on_unverified_rendered_job():
    result = _passed_result(1, status=JobStatus.passed_filters)
    with pytest.raises(InvariantViolation):
        build_digest([result], get_settings())


# --------------------------------------------------------------------------- #
# Email enforcement
# --------------------------------------------------------------------------- #


def test_email_refuses_non_shippable_digest():
    calls: list[dict] = []

    def transport(method, url, *, headers, json):
        calls.append(json)
        return type("R", (), {"status_code": 200, "text": "ok"})()

    digest = Digest(
        subject="x",
        sections=[
            DigestSection(
                title="Worth a look",
                items=[DigestItem(job_id=1, status=JobStatus.rejected.value, filter_passed=False)],
            )
        ],
    )
    emailer = ResendEmailer("k", "a@b.c", "d@e.f", transport=transport)
    with pytest.raises(InvariantViolation):
        send_digest(digest, get_settings(), emailer=emailer)
    assert calls == []


# --------------------------------------------------------------------------- #
# End-to-end: a high evaluator score cannot ship a filter-rejected job
# --------------------------------------------------------------------------- #


def test_high_score_cannot_ship_a_filter_rejected_job():
    settings = get_settings()
    conn = connect(":memory:")
    init_db(conn)
    profile = CandidateProfile(
        total_experience_months=0,
        experience_level=ExperienceLevel.fresher,
        approved=True,
    )
    save_profile(conn, profile)
    run_id = start_run(conn, "test")
    job_id, _ = upsert_job(
        conn,
        run_id,
        Job(
            source="t",
            url="http://x/1",
            dedupe_key="k1",
            title="Senior Software Engineer",
            company="C",
            description_raw="Senior Software Engineer. 5+ years of experience required.",
        ),
    )
    client = make_client(
        parsed={
            "min_years_experience": 5,
            "title_seniority": "senior",
            "must_have_skills": [],
            "confidence": 0.95,
        },
        verdict={"fit_score": 99, "recommendation": "strong_apply", "reasons_for": ["great"]},
        verifier={"veto": False},
    )
    results = run_pipeline(conn, run_id, profile, settings, client)
    assert results and results[0].status == JobStatus.rejected
    assert get_job_status(conn, job_id) == JobStatus.rejected
    digest = build_digest(results, settings, run_id=run_id)
    assert digest.is_empty()
    conn.close()


# --------------------------------------------------------------------------- #
# Property: sent ⊆ passed for random outcomes
# --------------------------------------------------------------------------- #


@given(st.lists(st.booleans(), min_size=0, max_size=12))
def test_property_sent_subset_of_passed(passed_flags: list[bool]):
    results: list[ProcessedJob] = []
    passed_ids: set[int] = set()
    for i, passed in enumerate(passed_flags, start=1):
        if passed:
            result = _passed_result(i)
            passed_ids.add(i)
        else:
            result = ProcessedJob(
                job_id=i,
                job=_job(i),
                filter_result=FilterResult(
                    passed=False,
                    rejections=[FilterRejection(rule_id="r", evidence="e")],
                ),
                final_recommendation=Recommendation.skip,
                status=JobStatus.rejected,
            )
        results.append(result)
    digest = build_digest(results, get_settings())
    assert set(digest.job_ids()) <= passed_ids
