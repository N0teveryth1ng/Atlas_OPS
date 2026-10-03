"""Verifier Agent — adversarial second pass (plan section 5.9).

Prompted to argue against applying. Uses a different model from the evaluator
(``settings.models.verifier``) for independence. May downgrade or veto; a veto
overrides the evaluator.
"""

from __future__ import annotations

import logging

from .config import Settings, get_settings
from .evaluator import (
    MAX_EVIDENCE_RETRIES,
    build_user_prompt,
    inputs_hash,
    job_source_text,
    profile_source_text,
)
from .evidence import (
    EvidenceValidationError,
    validate_verifier_evidence,
    validation_retry_message,
)
from .llm import LLMClient
from .prompt_store import load_prompt
from .schemas import (
    CandidateProfile,
    Job,
    ParsedJD,
    Recommendation,
    Verdict,
    VerifierVerdict,
)
from .skills import SkillMatch

logger = logging.getLogger(__name__)

_RECOMMENDATION_RANK = {
    Recommendation.skip: 0,
    Recommendation.maybe: 1,
    Recommendation.apply: 2,
    Recommendation.strong_apply: 3,
}


def verify_job(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    verdict: Verdict,
    skill_match: SkillMatch,
    settings: Settings | None = None,
    cache: dict | None = None,
) -> VerifierVerdict:
    settings = settings or get_settings()
    system, version = load_prompt("verifier")
    user = (
        build_user_prompt(profile, job, parsed_jd, skill_match)
        + "\n\n## Evaluator verdict (challenge this)\n"
        + verdict.model_dump_json(indent=2)
    )
    key = inputs_hash(
        "verifier",
        version,
        settings.models.verifier,
        job.dedupe_key or job.url,
        verdict.model_dump_json(),
    )
    if cache is not None and key in cache:
        return cache[key]

    logger.info("Verifier: challenging '%s' @ '%s'", job.title, job.company)
    jd_text = job_source_text(job)
    profile_text = profile_source_text(profile)
    errors: list[str] = []
    for attempt in range(MAX_EVIDENCE_RETRIES + 1):
        prompt = user if attempt == 0 else f"{user}\n\n{validation_retry_message(errors)}"
        result = client.call_json(
            schema=VerifierVerdict,
            system=system,
            user=prompt,
            model=settings.models.verifier,
        )
        errors = validate_verifier_evidence(result, jd_text, profile_text)
        if not errors:
            if cache is not None:
                cache[key] = result
            return result
        logger.warning(
            "Verifier evidence validation failed (attempt %d/%d): %s",
            attempt + 1,
            MAX_EVIDENCE_RETRIES + 1,
            errors,
        )
    raise EvidenceValidationError(errors)


def resolve_recommendation(verdict: Verdict, verifier: VerifierVerdict) -> Recommendation:
    """Apply veto/downgrade. The verifier can only lower the recommendation."""
    if verifier.veto:
        return Recommendation.skip
    final = verdict.recommendation
    if verifier.downgrade_to is not None:
        if _RECOMMENDATION_RANK[verifier.downgrade_to] < _RECOMMENDATION_RANK[final]:
            final = verifier.downgrade_to
    return final
