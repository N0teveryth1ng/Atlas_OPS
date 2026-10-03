"""Compact text renderers used to build LLM prompts (evaluator/verifier)."""

from __future__ import annotations

import json

from .schemas import CandidateProfile, Job, ParsedJD
from .skills import SkillMatch


def render_profile(profile: CandidateProfile) -> str:
    lines = [
        f"Experience: {profile.total_experience_months} months ({profile.experience_level.value})",
        f"Target roles: {', '.join(profile.target_roles) or '-'}",
    ]
    if profile.skills:
        lines.append(
            "Skills: " + ", ".join(f"{s.name} ({s.proficiency.value})" for s in profile.skills)
        )
    for project in profile.projects:
        detail = f"Project: {project.title or '-'} [{', '.join(project.tech)}]"
        if project.summary:
            detail += f" - {project.summary}"
        if project.outcomes:
            detail += f" (outcomes: {project.outcomes})"
        lines.append(detail)
    experiences = [
        f"{e.role or '-'} @ {e.company or '-'}" for e in (*profile.internships, *profile.work)
    ]
    if experiences:
        lines.append("Experience history: " + "; ".join(experiences))
    return "\n".join(lines)


def render_parsed_jd(parsed: ParsedJD) -> str:
    return json.dumps(parsed.model_dump(exclude_none=True), indent=2, default=str)


def render_skill_match(match: SkillMatch) -> str:
    return (
        f"must_have_coverage={match.must_have_coverage}\n"
        f"nice_to_have_coverage={match.nice_to_have_coverage}\n"
        f"missing_must_haves={match.missing_must_haves}\n"
        f"matched={match.matched}"
    )


def render_job(job: Job) -> str:
    return (
        f"Title: {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {job.location}\n"
        f"URL: {job.url}\n\n"
        f"Description:\n{job.description_raw}"
    )
