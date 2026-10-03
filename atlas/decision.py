"""Decision Engine — turn a verified evaluation into APPLY / REVIEW / SKIP.

This is deterministic where it can be: skills coverage, education, logistics,
seniority vetoes and the final rule are pure functions. Only ``project_relevance``
and the adversarial ``decision_critic`` need an LLM, and the evaluator/verifier
verdicts are reused rather than recomputed (one extra dimension + one critic).

Every decision is fully auditable: dimension scores, weights, spreads, vetoes,
evidence and the prompt/model versions that produced it are stored on the
``Decision`` record.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Mapping

from .config import Settings, config_hash, get_settings
from .evaluator import inputs_hash, job_source_text, profile_source_text
from .evidence import (
    EvidenceValidationError,
    validate_critic,
    validate_project_relevance,
    validation_retry_message,
)
from .llm import LLMClient
from .prompt_store import load_prompt
from .render import render_job, render_parsed_jd, render_profile, render_skill_match
from .schemas import (
    CandidateProfile,
    Decision,
    DecisionCritic,
    DecisionOutcome,
    Job,
    ParsedJD,
    ProjectRelevance,
    Recommendation,
    RemoteType,
    Verdict,
    VerifierVerdict,
    Veto,
)
from .skills import SkillMatch

logger = logging.getLogger(__name__)

MAX_EVIDENCE_RETRIES = 2

#: Every dimension the aggregate score sums; weights keys must match these.
DIMENSION_NAMES = (
    "skills_core",
    "seniority_fit",
    "role_fit",
    "project_relevance",
    "skills_secondary",
    "education_fit",
    "growth_fit",
    "logistics_fit",
)

#: Candidate may fall this many years below a stated minimum before it vetoes.
EXPERIENCE_TOLERANCE_YEARS = 1.0

#: Neutral score used when there is nothing to judge (absence != failure).
NEUTRAL_PROJECT_SCORE = 50.0
NEUTRAL_EDUCATION_SCORE = 100.0
NEUTRAL_LOGISTICS_SCORE = 100.0

_DEGREE_WORDS = ("bachelor", "b.tech", "btech", "b.e", "bsc", "master", "m.tech", "msc", "phd")


# --------------------------------------------------------------------------- #
# Pure dimension scores
# --------------------------------------------------------------------------- #


def skills_core_score(skill_match: SkillMatch) -> float:
    return round(skill_match.must_have_coverage * 100.0, 4)


def skills_secondary_score(skill_match: SkillMatch) -> float:
    return round(skill_match.nice_to_have_coverage * 100.0, 4)


def education_score(parsed: ParsedJD, profile: CandidateProfile) -> float:
    """100 when no degree is required; otherwise score candidate education."""
    if not parsed.education_required:
        return NEUTRAL_EDUCATION_SCORE
    degree_text = " ".join((edu.degree or "") for edu in profile.education).lower()
    if not degree_text.strip():
        return 0.0
    if any(word in degree_text for word in _DEGREE_WORDS):
        return 100.0
    return 70.0


def logistics_score(parsed: ParsedJD) -> float:
    """Onsite hybrid/onsite and work-authorization blockers reduce fit."""
    score = NEUTRAL_LOGISTICS_SCORE
    if parsed.remote_type == RemoteType.onsite:
        score -= 40.0
    elif parsed.remote_type == RemoteType.hybrid:
        score -= 15.0
    if parsed.visa_relocation:
        text = parsed.visa_relocation.lower()
        if "no sponsorship" in text or "must be authorized" in text or "authorized to work" in text:
            score -= 30.0
    return max(0.0, score)


def risk_penalty(parsed: ParsedJD, settings: Settings) -> float:
    flags = parsed.red_flags or []
    return min(
        settings.decision.risk_penalty_per_flag * len(flags), settings.decision.max_risk_penalty
    )


def project_relevance_value(
    relevance: ProjectRelevance | None, profile: CandidateProfile
) -> tuple[float, bool]:
    """Return ``(score, measured)``. ``measured`` is False when nothing to judge."""
    if not profile.projects:
        return NEUTRAL_PROJECT_SCORE, False
    if relevance is None or not relevance.per_project:
        return NEUTRAL_PROJECT_SCORE, False
    return float(max(0.0, min(100.0, relevance.overall))), True


# --------------------------------------------------------------------------- #
# Vetoes
# --------------------------------------------------------------------------- #


def _candidate_years(profile: CandidateProfile) -> float:
    return profile.total_experience_months / 12.0


def _verifier_veto_has_evidence(verifier: VerifierVerdict) -> bool:
    return bool(verifier.reasons_against) and all(e.quote.strip() for e in verifier.reasons_against)


def evaluate_vetoes(
    *,
    parsed: ParsedJD,
    profile: CandidateProfile,
    verdict: Verdict,
    verifier: VerifierVerdict,
    skill_match: SkillMatch,
    critic: DecisionCritic | None,
    settings: Settings,
) -> list[Veto]:
    """Collect hard/soft vetoes in a stable order."""
    vetoes: list[Veto] = []
    decision = settings.decision

    if skill_match.must_have_coverage < decision.coverage_floor:
        vetoes.append(
            Veto(
                rule_id="coverage_floor",
                reason=(
                    f"must-have coverage {skill_match.must_have_coverage:.2f} below "
                    f"floor {decision.coverage_floor:.2f}"
                ),
                hard=True,
            )
        )

    years = _candidate_years(profile)
    if parsed.min_years_experience is not None:
        allowed = parsed.min_years_experience - EXPERIENCE_TOLERANCE_YEARS
        if years < allowed:
            vetoes.append(
                Veto(
                    rule_id="seniority_years",
                    reason=(
                        f"needs {parsed.min_years_experience:g}y (tolerance "
                        f"{EXPERIENCE_TOLERANCE_YEARS:g}y); candidate has {years:.1f}y"
                    ),
                    hard=True,
                )
            )

    if verdict.seniority_fit < decision.seniority_floor:
        vetoes.append(
            Veto(
                rule_id="seniority_fit",
                reason=(
                    f"seniority_fit {verdict.seniority_fit:.0f} below floor "
                    f"{decision.seniority_floor:.0f}"
                ),
                hard=True,
            )
        )

    if verifier.veto:
        vetoes.append(
            Veto(
                rule_id="verifier_veto",
                reason="verifier vetoed the application",
                hard=_verifier_veto_has_evidence(verifier),
                evidence=verifier.reasons_against[0] if verifier.reasons_against else None,
            )
        )
    elif verifier.downgrade_to == Recommendation.skip:
        vetoes.append(
            Veto(
                rule_id="verifier_downgrade",
                reason="verifier downgraded to skip",
                hard=False,
                evidence=verifier.reasons_against[0] if verifier.reasons_against else None,
            )
        )

    if critic is not None and critic.propose_veto:
        vetoes.append(
            Veto(
                rule_id="critic_veto",
                reason=critic.strongest_reason.quote if critic.strongest_reason else "critic veto",
                hard=critic.strongest_reason is not None,
                evidence=critic.strongest_reason,
            )
        )

    return vetoes


# --------------------------------------------------------------------------- #
# Score, confidence, rule
# --------------------------------------------------------------------------- #


def aggregate_score(
    dimensions: Mapping[str, float], weights: Mapping[str, float], penalty: float
) -> float:
    total = sum(dimensions.get(name, 0.0) * weights.get(name, 0.0) for name in DIMENSION_NAMES)
    return max(0.0, min(100.0, total - penalty))


def compute_confidence(
    *,
    parsed: ParsedJD,
    dimensions: Mapping[str, float],
    spreads: Mapping[str, float],
    score: float,
    verifier: VerifierVerdict,
    settings: Settings,
) -> float:
    decision = settings.decision
    parse_quality = max(0.0, min(1.0, parsed.confidence))

    measured_spreads = [spread for name, spread in spreads.items() if name in dimensions]
    mean_spread = statistics.fmean(measured_spreads) if measured_spreads else 0.0
    spread_quality = max(0.0, min(1.0, 1.0 - mean_spread / decision.spread_limit))

    agreement = 0.0 if verifier.veto else 1.0
    evidence_ok = 1.0
    margin = max(0.0, min(1.0, abs(score - decision.apply_threshold) / decision.margin_scale))

    return round(
        0.25 * parse_quality
        + 0.25 * spread_quality
        + 0.20 * agreement
        + 0.15 * evidence_ok
        + 0.15 * margin,
        4,
    )


def decision_rule(
    *, score: float, confidence: float, vetoes: list[Veto], settings: Settings
) -> DecisionOutcome:
    decision = settings.decision
    if any(veto.hard for veto in vetoes):
        return DecisionOutcome.skip
    if any(veto for veto in vetoes):  # only soft vetoes remain
        return DecisionOutcome.review
    if confidence < decision.c_min:
        return DecisionOutcome.review
    if score >= decision.apply_threshold and confidence >= decision.c_apply:
        return DecisionOutcome.apply
    if score >= decision.review_threshold:
        return DecisionOutcome.review
    return DecisionOutcome.skip


def decide(
    *,
    parsed: ParsedJD,
    profile: CandidateProfile,
    verdict: Verdict,
    verifier: VerifierVerdict,
    skill_match: SkillMatch,
    dimensions: Mapping[str, float],
    spreads: dict[str, float] | None = None,
    relevance: ProjectRelevance | None = None,
    critic: DecisionCritic | None = None,
    settings: Settings,
) -> Decision:
    """Pure decision assembly from already-computed LLM outputs."""
    spreads = spreads or {}
    weights = settings.decision.weights.model_dump()
    penalty = risk_penalty(parsed, settings)
    score = aggregate_score(dimensions, weights, penalty)
    confidence = compute_confidence(
        parsed=parsed,
        dimensions=dimensions,
        spreads=spreads,
        score=score,
        verifier=verifier,
        settings=settings,
    )
    vetoes = evaluate_vetoes(
        parsed=parsed,
        profile=profile,
        verdict=verdict,
        verifier=verifier,
        skill_match=skill_match,
        critic=critic,
        settings=settings,
    )
    outcome = decision_rule(score=score, confidence=confidence, vetoes=vetoes, settings=settings)

    reasons_for = list(verdict.reasons_for)
    if relevance is not None and relevance.per_project:
        best = max(relevance.per_project, key=lambda item: item.score)
        reasons_for.extend(best.profile_evidence)
        reasons_for.extend(best.jd_evidence)

    reasons_against = list(verdict.reasons_against) + list(verifier.reasons_against)
    if critic is not None:
        reasons_against.extend(critic.reasons_against)

    notes: list[str] = []
    if not profile.projects:
        notes.append("no projects/internships to assess; project_relevance neutral")

    return Decision(
        outcome=outcome,
        match_score=round(score, 4),
        confidence=confidence,
        dimensions={name: round(value, 4) for name, value in dimensions.items()},
        spreads={name: round(value, 4) for name, value in spreads.items()},
        weights=weights,
        risk_penalty=penalty,
        vetoes=vetoes,
        reasons_for=reasons_for,
        reasons_against=reasons_against,
        missing_skills=list(skill_match.missing_must_haves),
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# LLM orchestration
# --------------------------------------------------------------------------- #


def build_project_prompt(profile: CandidateProfile, job: Job, parsed: ParsedJD) -> str:
    return (
        "## Candidate profile (projects and internships)\n"
        f"{render_profile(profile)}\n\n"
        "## Parsed job requirements\n"
        f"{render_parsed_jd(parsed)}\n\n"
        "## Raw job posting\n"
        f"{render_job(job)}"
    )


def assess_projects(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed: ParsedJD,
    settings: Settings,
    cache: dict | None = None,
) -> tuple[ProjectRelevance | None, float]:
    """Sample ``project_relevance`` N times; return ``(best_median, spread)``."""
    if not profile.projects:
        return None, 0.0

    system, version = load_prompt("project_relevance")
    user = build_project_prompt(profile, job, parsed)
    jd_text = job_source_text(job)
    profile_text = profile_source_text(profile)

    samples = max(1, settings.decision.samples)
    overalls: list[float] = []
    first: ProjectRelevance | None = None
    for _ in range(samples):
        key = inputs_hash(
            "project_relevance",
            version,
            settings.models.evaluator,
            job.dedupe_key or job.url,
            parsed.model_dump_json(),
            profile.model_dump_json(),
            str(len(overalls)),
        )
        if cache is not None and key in cache:
            relevance = cache[key]
        else:
            relevance = _call_with_evidence_retry(
                client,
                schema=ProjectRelevance,
                system=system,
                user=user,
                model=settings.models.evaluator,
                jd_text=jd_text,
                profile_text=profile_text,
                validator=validate_project_relevance,
            )
            if cache is not None:
                cache[key] = relevance
        if first is None:
            first = relevance
        overalls.append(float(relevance.overall))

    spread = max(overalls) - min(overalls)
    median = statistics.median(overalls)
    if first is None:  # pragma: no cover - guarded by the empty-profile early return
        return None, spread
    return first.model_copy(update={"overall": median}), spread


def run_critic(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed: ParsedJD,
    verdict: Verdict,
    skill_match: SkillMatch,
    settings: Settings,
    cache: dict | None = None,
) -> DecisionCritic:
    system, version = load_prompt("decision_critic")
    user = (
        "## Candidate profile\n"
        f"{render_profile(profile)}\n\n"
        "## Parsed job requirements\n"
        f"{render_parsed_jd(parsed)}\n\n"
        "## Deterministic skill match\n"
        f"{render_skill_match(skill_match)}\n\n"
        "## Evaluator verdict (challenge this)\n"
        f"{verdict.model_dump_json(indent=2)}\n\n"
        "## Raw job posting\n"
        f"{render_job(job)}"
    )
    key = inputs_hash(
        "decision_critic",
        version,
        settings.models.verifier,
        job.dedupe_key or job.url,
        parsed.model_dump_json(),
        verdict.model_dump_json(),
    )
    if cache is not None and key in cache:
        return cache[key]
    critic = _call_with_evidence_retry(
        client,
        schema=DecisionCritic,
        system=system,
        user=user,
        model=settings.models.verifier,
        jd_text=job_source_text(job),
        profile_text=profile_source_text(profile),
        validator=validate_critic,
    )
    if cache is not None:
        cache[key] = critic
    return critic


def _call_with_evidence_retry(
    client: LLMClient,
    *,
    schema,
    system: str,
    user: str,
    model: str,
    jd_text: str,
    profile_text: str,
    validator,
):
    errors: list[str] = []
    for attempt in range(MAX_EVIDENCE_RETRIES + 1):
        prompt = user if attempt == 0 else f"{user}\n\n{validation_retry_message(errors)}"
        result = client.call_json(schema=schema, system=system, user=prompt, model=model)
        errors = validator(result, jd_text, profile_text)
        if not errors:
            return result
        logger.warning(
            "Decision evidence validation failed (attempt %d/%d): %s",
            attempt + 1,
            MAX_EVIDENCE_RETRIES + 1,
            errors,
        )
    raise EvidenceValidationError(errors)


def evaluate_decision(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed: ParsedJD,
    verdict: Verdict,
    verifier: VerifierVerdict,
    skill_match: SkillMatch,
    settings: Settings | None = None,
    cache: dict | None = None,
) -> Decision:
    """Full decision engine: code dims + LLM dims/critic + deterministic rule."""
    settings = settings or get_settings()

    relevance, project_spread = assess_projects(
        client, profile=profile, job=job, parsed=parsed, settings=settings, cache=cache
    )
    critic = run_critic(
        client,
        profile=profile,
        job=job,
        parsed=parsed,
        verdict=verdict,
        skill_match=skill_match,
        settings=settings,
        cache=cache,
    )

    project_value, measured = project_relevance_value(relevance, profile)
    dimensions = {
        "skills_core": skills_core_score(skill_match),
        "skills_secondary": skills_secondary_score(skill_match),
        "seniority_fit": float(verdict.seniority_fit),
        "role_fit": float(verdict.role_fit),
        "growth_fit": float(verdict.growth_fit),
        "project_relevance": project_value,
        "education_fit": education_score(parsed, profile),
        "logistics_fit": logistics_score(parsed),
    }
    spreads = {"project_relevance": project_spread} if measured else {}

    decision = decide(
        parsed=parsed,
        profile=profile,
        verdict=verdict,
        verifier=verifier,
        skill_match=skill_match,
        dimensions=dimensions,
        spreads=spreads,
        relevance=relevance,
        critic=critic,
        settings=settings,
    )
    decision.config_hash = config_hash()
    decision.prompt_versions = {
        "evaluator": "reused",
        "verifier": "reused",
        "project_relevance": _version_of("project_relevance"),
        "decision_critic": _version_of("decision_critic"),
    }
    decision.model_names = {
        "evaluator": settings.models.evaluator,
        "verifier": settings.models.verifier,
    }
    return decision


def _version_of(name: str) -> str:
    _text, version = load_prompt(name)
    return version
