"""Query Planner (plan section 5.2).

Turns the approved profile + config preferences into a diverse, deduped list of
target-role queries. Level terms are always included so entry-level roles are
not missed.
"""

from __future__ import annotations

from .config import Settings
from .schemas import CandidateProfile

LEVEL_TERMS = [
    "junior",
    "fresher",
    "graduate",
    "entry level",
    "associate",
]

MAX_QUERIES = 30


def plan_queries(profile: CandidateProfile, settings: Settings) -> list[str]:
    roles = list(profile.target_roles) or list(settings.targets.target_roles)
    if not roles:
        roles = ["software engineer"]

    candidates: list[str] = []
    for role in roles[:5]:
        role = role.strip()
        if not role:
            continue
        candidates.append(role)
        for term in LEVEL_TERMS:
            candidates.append(f"{term} {role}")

    top_skills = [skill.name for skill in profile.skills[:3] if skill.name]
    for role in roles[:2]:
        for skill in top_skills[:2]:
            candidates.append(f"{skill} {role}")

    for location in settings.targets.locations[:2]:
        candidates.append(f"{roles[0]} {location}")

    seen: set[str] = set()
    queries: list[str] = []
    for candidate in candidates:
        key = candidate.lower().strip()
        if key and key not in seen:
            seen.add(key)
            queries.append(candidate.strip())
    return queries[:MAX_QUERIES]
