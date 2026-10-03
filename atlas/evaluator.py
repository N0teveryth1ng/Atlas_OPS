"""Evaluator Agent (plan section 5.8).

Runs only on jobs that survived the deterministic filters. Produces a
schema-validated ``Verdict`` with evidence-citing reasons, at temperature 0, and
caches by a hash of the inputs so the same job never costs a second call.
"""

from __future__ import annotations

import hashlib
import logging

from .config import Settings, get_settings
from .evidence import (
    EvidenceValidationError,
    validate_verdict_evidence,
    validation_retry_message,
)
from .llm import LLMClient
from .prompt_store import load_prompt
from .render import render_job, render_parsed_jd, render_profile, render_skill_match
from .schemas import CandidateProfile, Job, ParsedJD, Verdict
from .skills import SkillMatch

logger = logging.getLogger(__name__)

MAX_EVIDENCE_RETRIES = 2


def job_source_text(job: Job) -> str:
    """The 'jd' text that evidence quotes must appear in."""
    return f"{job.title or ''}\n{job.description_raw or ''}"


def profile_source_text(profile: CandidateProfile) -> str:
    """The 'profile' text that evidence quotes must appear in."""
    return render_profile(profile)


def inputs_hash(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x1f")
    return digest.hexdigest()[:32]


def build_user_prompt(
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    skill_match: SkillMatch,
) -> str:
    return (
        "## Candidate profile\n"
        f"{render_profile(profile)}\n\n"
        "## Parsed job requirements\n"
        f"{render_parsed_jd(parsed_jd)}\n\n"
        "## Deterministic skill match\n"
        f"{render_skill_match(skill_match)}\n\n"
        "## Raw job posting\n"
        f"{render_job(job)}"
    )


def evaluate_job(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    skill_match: SkillMatch,
    settings: Settings | None = None,
    cache: dict | None = None,
) -> Verdict:
    settings = settings or get_settings()
    system, version = load_prompt("evaluator")
    user = build_user_prompt(profile, job, parsed_jd, skill_match)
    key = inputs_hash(
        "evaluator",
        version,
        settings.models.evaluator,
        str(profile.version),
        job.dedupe_key or job.url,
        parsed_jd.model_dump_json(),
        str(skill_match.must_have_coverage),
        str(skill_match.missing_must_haves),
    )
    if cache is not None and key in cache:
        return cache[key]

    logger.info("Evaluator: scoring '%s' @ '%s'", job.title, job.company)
    jd_text = job_source_text(job)
    profile_text = profile_source_text(profile)
    errors: list[str] = []
    for attempt in range(MAX_EVIDENCE_RETRIES + 1):
        prompt = user if attempt == 0 else f"{user}\n\n{validation_retry_message(errors)}"
        verdict = client.call_json(
            schema=Verdict,
            system=system,
            user=prompt,
            model=settings.models.evaluator,
        )
        errors = validate_verdict_evidence(verdict, jd_text, profile_text)
        if not errors:
            if cache is not None:
                cache[key] = verdict
            return verdict
        logger.warning(
            "Evaluator evidence validation failed (attempt %d/%d): %s",
            attempt + 1,
            MAX_EVIDENCE_RETRIES + 1,
            errors,
        )
    raise EvidenceValidationError(errors)
