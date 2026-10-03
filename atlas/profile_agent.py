"""Profile Agent — resume and/or free-text description -> CandidateProfile.

Implements section 5.0 / 5.1 of the rework plan:

* resume input, description input, or combined (merge),
* explicit description statements override resume *preferences*,
* the resume wins for *factual history* (education, experience, skills),
* experience level is derived by code from months, never guessed,
* anything uncertain goes to ``ambiguities`` for human confirmation.
"""

from __future__ import annotations

import logging

from .llm import LLMClient
from .pdf import extract_text
from .prompt_store import load_prompt
from .schemas import (
    CandidateProfile,
    ExperienceLevel,
    InputMode,
    ProfileExtraction,
    Proficiency,
    Skill,
)

logger = logging.getLogger(__name__)


def normalize_skill_name(name: str) -> str:
    """Phase-1 normalisation; Phase 3 replaces this with the skill ontology."""
    return " ".join(name.lower().split())


def derive_experience_level(total_experience_months: int) -> ExperienceLevel:
    """Derive level from months (code, not the LLM)."""
    if total_experience_months < 12:
        return ExperienceLevel.fresher
    if total_experience_months < 36:
        return ExperienceLevel.junior
    if total_experience_months < 84:
        return ExperienceLevel.mid
    return ExperienceLevel.senior


def _extraction_to_profile(
    extraction: ProfileExtraction, mode: InputMode, *, cap_proficiency: bool = False
) -> CandidateProfile:
    skills: list[Skill] = []
    for raw in extraction.skills:
        if not raw.name or not raw.name.strip():
            continue
        proficiency = raw.proficiency
        if cap_proficiency and proficiency == Proficiency.strong:
            proficiency = Proficiency.familiar
        skills.append(
            Skill(
                name=raw.name.strip(),
                canonical_name=normalize_skill_name(raw.name),
                category=raw.category,
                proficiency=proficiency,
                evidence=raw.evidence,
                last_used=raw.last_used,
            )
        )

    months = extraction.total_experience_months or 0
    ambiguities = list(extraction.ambiguities)

    if months > 0:
        level = derive_experience_level(months)
    elif extraction.experience_level_stated:
        level = extraction.experience_level_stated
    else:
        level = ExperienceLevel.fresher
        if mode == InputMode.description and "experience_level_unknown" not in ambiguities:
            ambiguities.append("experience_level_unknown")

    return CandidateProfile(
        source_mode=mode,
        full_name=extraction.full_name,
        email=extraction.email,
        links=extraction.links,
        total_experience_months=months,
        experience_level=level,
        education=extraction.education,
        skills=skills,
        projects=extraction.projects,
        internships=extraction.internships,
        work=extraction.work,
        target_roles=extraction.target_roles,
        certifications=extraction.certifications,
        ambiguities=ambiguities,
    )


def extract_from_resume(client: LLMClient, resume_source) -> ProfileExtraction:
    text = extract_text(resume_source)
    system, version = load_prompt("profile_from_resume")
    logger.info("Profile Agent: parsing resume (prompt v%s)", version)
    return client.call_json(
        schema=ProfileExtraction,
        system=system,
        user=f"Resume:\n\n{text}",
    )


def extract_from_description(client: LLMClient, description: str) -> ProfileExtraction:
    system, version = load_prompt("profile_from_description")
    logger.info("Profile Agent: parsing description (prompt v%s)", version)
    return client.call_json(
        schema=ProfileExtraction,
        system=system,
        user=f"Self-description:\n\n{description}",
    )


def merge_profiles(resume: CandidateProfile, description: CandidateProfile) -> CandidateProfile:
    """Merge per section 5.0: description overrides preferences; resume wins history."""
    merged = resume.model_copy(deep=True)
    merged.source_mode = InputMode.combined

    # Preferences from the description override the resume.
    if description.target_roles:
        merged.target_roles = description.target_roles

    # Factual history: resume wins. Fill only genuine gaps.
    merged.full_name = merged.full_name or description.full_name
    merged.email = merged.email or description.email
    merged.links.github = merged.links.github or description.links.github
    merged.links.linkedin = merged.links.linkedin or description.links.linkedin
    merged.links.portfolio = merged.links.portfolio or description.links.portfolio
    if not merged.education:
        merged.education = description.education
    if not merged.skills:
        merged.skills = description.skills
    if not merged.projects:
        merged.projects = description.projects
    if not merged.internships:
        merged.internships = description.internships
    if not merged.work:
        merged.work = description.work

    merged.certifications = sorted(set(resume.certifications) | set(description.certifications))
    merged.ambiguities = sorted(set(resume.ambiguities) | set(description.ambiguities))
    return merged


def build_profile(
    client: LLMClient,
    *,
    resume_source=None,
    description: str | None = None,
) -> CandidateProfile:
    """Build a profile from a resume, a description, or both."""
    has_resume = resume_source is not None
    has_description = bool(description and description.strip())

    if has_resume and has_description:
        resume_profile = _extraction_to_profile(extract_from_resume(client, resume_source), InputMode.resume)
        description_profile = _extraction_to_profile(
            extract_from_description(client, description), InputMode.description, cap_proficiency=True
        )
        return merge_profiles(resume_profile, description_profile)
    if has_resume:
        return _extraction_to_profile(extract_from_resume(client, resume_source), InputMode.resume)
    if has_description:
        return _extraction_to_profile(
            extract_from_description(client, description), InputMode.description, cap_proficiency=True
        )
    raise ValueError("Provide a resume, a description, or both.")


def render_profile_summary(profile: CandidateProfile) -> str:
    """Human-readable summary for the review/approval step."""
    lines = [
        f"Mode: {profile.source_mode.value}   Approved: {profile.approved}",
        f"Name: {profile.full_name}   Email: {profile.email}",
        f"Experience: {profile.total_experience_months} months -> {profile.experience_level.value}",
        f"Target roles: {', '.join(profile.target_roles) or '-'}",
        f"Skills ({len(profile.skills)}): "
        + ", ".join(f"{s.name}[{s.proficiency.value}]" for s in profile.skills),
        f"Education: "
        + "; ".join(
            " ".join(filter(None, [e.degree, e.field, e.institution])) or "-"
            for e in profile.education
        ),
    ]
    if profile.ambiguities:
        lines.append("Ambiguities needing confirmation:")
        lines.extend(f"  - {a}" for a in profile.ambiguities)
    missing = profile.missing_critical_fields()
    if missing:
        lines.append(f"Missing critical fields: {', '.join(missing)}")
    return "\n".join(lines)
