"""Ranker (plan section 5.10) — weighted final score."""

from __future__ import annotations

from .config import Settings, get_settings
from .job_status import assert_shippable
from .schemas import Job, Recommendation, Verdict
from .skills import SkillMatch


def preference_bonus(job: Job, settings: Settings) -> float:
    company = (job.company or "").lower()
    if company and any(company == target.lower() for target in settings.companies.target_companies):
        return 100.0
    return 0.0


def final_score(
    verdict: Verdict,
    skill_match: SkillMatch,
    job: Job,
    settings: Settings | None = None,
) -> float:
    settings = settings or get_settings()
    weights = settings.ranking.weights
    score = (
        verdict.fit_score * weights.fit_score
        + skill_match.must_have_coverage * 100 * weights.must_have_coverage
        + verdict.seniority_fit * weights.seniority_fit
        + preference_bonus(job, settings) * weights.preference_bonus
    )
    return round(max(0.0, min(100.0, score)), 2)


def rank(results: list, *, include_maybe: bool = True) -> list:
    """Sort processed jobs by score, dropping hard skips.

    Enforces the audit D-4/L8 invariant inside the ranker itself: a job that did
    not pass the hard filters (or lacks a stored filter result) can never be
    ranked, even if a caller bypasses the pipeline and invokes ``rank`` directly.
    """
    allowed = {Recommendation.apply, Recommendation.strong_apply}
    if include_maybe:
        allowed.add(Recommendation.maybe)
    filtered = []
    for result in results:
        if result.final_recommendation not in allowed:
            continue
        filter_passed = result.filter_result is not None and result.filter_result.passed
        assert_shippable(result.status, filter_passed)
        filtered.append(result)
    return sorted(filtered, key=lambda r: r.score, reverse=True)
