import pytest

from atlas.config import get_settings
from atlas.digest import build_digest, render_html, render_text
from atlas.job_status import InvariantViolation, JobStatus
from atlas.pipeline import ProcessedJob
from atlas.schemas import (
    FilterRejection,
    FilterResult,
    Job,
    Recommendation,
    Verdict,
    VerifierVerdict,
)
from atlas.skills import SkillMatch


def _job(i: int, title: str = "Junior Python Dev", company: str = "C") -> Job:
    return Job(
        source="t",
        url=f"http://x/{i}",
        dedupe_key=f"k{i}",
        title=title,
        company=company,
        location="Remote",
    )


def _result(i, rec, score=80.0, needs_review=False, status=JobStatus.ranked):
    verdict = Verdict(
        fit_score=score,
        seniority_fit=90,
        recommendation=rec,
        reasons_for=["JD asks for 0-2 years", "Python core stack"],
        reasons_against=["On-call rotation"],
        seniority_assessment="Open to a fresher per the JD.",
    )
    return ProcessedJob(
        job_id=i,
        job=_job(i),
        filter_result=FilterResult(passed=True),
        skill_match=SkillMatch(must_have_coverage=0.8, nice_to_have_coverage=0.0, missing_must_haves=["Docker"]),
        verdict=verdict,
        verifier=VerifierVerdict(),
        final_recommendation=rec,
        score=score,
        needs_review=needs_review,
        status=status,
    )


def test_build_digest_sections():
    results = [
        _result(1, Recommendation.strong_apply, 90),
        _result(2, Recommendation.apply, 70),
    ]
    digest = build_digest(results, get_settings())
    titles = [section.title for section in digest.sections]
    assert "Strong matches" in titles
    assert "Worth a look" in titles
    assert digest.summary.sent == 2
    assert digest.item_count == 2


def test_build_digest_raises_on_needs_review():
    results = [_result(3, Recommendation.maybe, 50, needs_review=True, status=JobStatus.needs_review)]
    with pytest.raises(InvariantViolation):
        build_digest(results, get_settings())


def test_build_digest_summary_counts_rejections():
    rejected = ProcessedJob(
        job_id=9,
        job=_job(9),
        filter_result=FilterResult(
            passed=False,
            rejections=[FilterRejection(rule_id="senior_title", evidence="senior")],
        ),
        final_recommendation=Recommendation.skip,
    )
    digest = build_digest([_result(1, Recommendation.apply), rejected], get_settings())
    assert digest.summary.filtered_out == 1
    assert digest.summary.rejection_counts["senior_title"] == 1


def test_build_digest_skips_already_sent():
    digest = build_digest([_result(1, Recommendation.apply)], get_settings(), already_sent={1})
    assert digest.is_empty()


def test_render_contains_key_fields():
    digest = build_digest([_result(1, Recommendation.apply)], get_settings())
    text = render_text(digest)
    html = render_html(digest)
    assert "Junior Python Dev" in text
    assert "Docker" in text
    assert "Seniority:" in html
    assert "Risk:" in html
