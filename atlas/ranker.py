"""Ranker (plan section 5.10) — weighted final score."""

from __future__ import annotations

from .config import Settings, get_settings
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
    """Sort processed jobs by score, dropping hard skips."""
    allowed = {Recommendation.apply, Recommendation.strong_apply}
    if include_maybe:
        allowed.add(Recommendation.maybe)
    filtered = [r for r in results if r.final_recommendation in allowed]
    return sorted(filtered, key=lambda r: r.score, reverse=True)
