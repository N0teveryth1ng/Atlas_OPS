"""Hard filter — pure code, zero LLM.

Section 5.6 of the rework plan. Deterministic and cheap: it runs before any LLM
scoring, and its rejections can never be overridden by a high keyword score.
Every rejection carries a machine-readable ``rule_id`` and evidence.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .config import Settings
from .jd_parser import SENIORITY_RANK, detect_title_seniority
from .schemas import (
    CandidateProfile,
    FilterRejection,
    FilterResult,
    Job,
    ParsedJD,
    RemoteType,
    Seniority,
)

logger = logging.getLogger(__name__)

DISALLOWED_SENIORITY = {
    Seniority.senior,
    Seniority.lead,
    Seniority.principal,
    Seniority.architect,
    Seniority.manager,
}

_SALARY_RE = re.compile(r"(\d[\d,\.]*)\s*(k|m|lpa|lakh|lakhs|per annum|/yr|/year)?", re.I)


def _parse_salary(text: str) -> float | None:
    """Best-effort annual figure. Currency-agnostic; returns None if unsure."""
    match = _SALARY_RE.search(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (match.group(2) or "").lower()
    if unit == "k":
        value *= 1_000
    elif unit == "m":
        value *= 1_000_000
    elif unit in {"lpa", "lakh", "lakhs"}:
        value *= 100_000
    return value


def apply_hard_filters(
    job: Job,
    parsed: ParsedJD,
    profile: CandidateProfile,
    settings: Settings,
) -> FilterResult:
    """Apply every hard rule; return the pass/fail verdict plus reasons."""
    rejections: list[FilterRejection] = []

    # 1. Required experience exceeds what the candidate qualifies for.
    my_years = settings.candidate.my_years_experience
    allowed = my_years + settings.candidate.experience_tolerance
    if parsed.min_years_experience is not None and parsed.min_years_experience > allowed:
        rejections.append(
            FilterRejection(
                rule_id="too_much_experience",
                evidence=(
                    f"requires {parsed.min_years_experience:g} yr(s); allowed up to {allowed:g} "
                    f"[{(parsed.years_source_quote or 'no quote')}]"
                ),
            )
        )

    # 2. Senior/lead/manager title.
    title_level = parsed.title_seniority
    raw_level = detect_title_seniority(job.title or "")
    if SENIORITY_RANK[raw_level] > SENIORITY_RANK[title_level]:
        title_level = raw_level
    if title_level in DISALLOWED_SENIORITY:
        rejections.append(
            FilterRejection(rule_id="senior_title", evidence=f"title seniority={title_level.value}")
        )

    # 3. Location / remote preference.
    if parsed.remote_type == RemoteType.remote and not settings.targets.remote_ok:
        rejections.append(
            FilterRejection(rule_id="location_mismatch", evidence="remote role but remote not allowed")
        )
    if settings.targets.locations and parsed.remote_type != RemoteType.remote:
        haystack = f"{job.location or ''} {parsed.location or ''}".lower()
        if not any(pref.lower() in haystack for pref in settings.targets.locations):
            rejections.append(
                FilterRejection(
                    rule_id="location_mismatch",
                    evidence=f"location '{job.location or parsed.location}' not in {settings.targets.locations}",
                )
            )

    # 4. Employment type.
    if parsed.employment_type and settings.targets.employment_types:
        et = parsed.employment_type.lower()
        if not any(
            wanted.lower() in et or et in wanted.lower()
            for wanted in settings.targets.employment_types
        ):
            rejections.append(
                FilterRejection(
                    rule_id="employment_type_mismatch",
                    evidence=f"'{parsed.employment_type}' not in {settings.targets.employment_types}",
                )
            )

    # 5. Blacklisted company.
    company = (job.company or "").strip().lower()
    if company and any(
        company == blocked.lower() or blocked.lower() in company
        for blocked in settings.companies.blacklist_companies
    ):
        rejections.append(
            FilterRejection(rule_id="blacklisted_company", evidence=f"company='{job.company}'")
        )

    # 6. Salary below floor (only when explicitly stated).
    if settings.targets.salary_floor and parsed.salary:
        value = _parse_salary(parsed.salary)
        if value is not None and value < settings.targets.salary_floor:
            rejections.append(
                FilterRejection(
                    rule_id="salary_below_floor",
                    evidence=f"'{parsed.salary}' below floor {settings.targets.salary_floor}",
                )
            )

    # 7. Missing a deal-breaker skill the job requires.
    if settings.filters.deal_breaker_skills:
        must = {s.lower().strip() for s in parsed.must_have_skills}
        for skill in settings.filters.deal_breaker_skills:
            if skill.lower().strip() in must:
                rejections.append(
                    FilterRejection(rule_id="deal_breaker_skill", evidence=f"requires '{skill}'")
                )

    # 8. Stale posting.
    if job.posted_at and settings.filters.max_job_age_days:
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - posted).days
        if age_days > settings.filters.max_job_age_days:
            rejections.append(
                FilterRejection(rule_id="stale_job", evidence=f"posted {age_days} days ago")
            )

    return FilterResult(passed=not rejections, rejections=rejections)
