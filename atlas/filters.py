"""Hard filter — pure code, zero LLM.

Section 5.6 of the rework plan. Deterministic and cheap: it runs before any LLM
scoring, and its rejections can never be overridden by a high keyword score.
Every rejection carries a machine-readable ``rule_id`` and evidence.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from .config import Settings
from .jd_parser import SENIORITY_RANK, detect_title_seniority
from .schemas import (
    CandidateProfile,
    FilterCheck,
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

#: Title phrases that mark a posting as NOT a specific job opening: talent-pool
#: and general-application boilerplate, speculative/expression-of-interest ads.
#: Matched case-insensitively as substrings of the title; the matched phrase is
#: recorded as the rejection evidence.
NON_JOB_LISTING_PHRASES: tuple[str, ...] = (
    "talent community",
    "talent pool",
    "join our talent",
    "initiativbewerbung",
    "spontaneous application",
    "unsolicited application",
    "general application",
    "expression of interest",
    "future opportunities",
)

_SALARY_RE = re.compile(r"(\d[\d,\.]*)\s*(k|m|lpa|lakh|lakhs|per annum|/yr|/year)?", re.IGNORECASE)


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


def _check(
    rule_id: str,
    *,
    checked: bool,
    value: str | None = None,
    limit: str | None = None,
    skipped_reason: str | None = None,
) -> FilterCheck:
    return FilterCheck(
        rule_id=rule_id,
        checked=checked,
        value=value,
        limit=limit,
        skipped_reason=skipped_reason,
    )


def apply_hard_filters(
    job: Job,
    parsed: ParsedJD,
    profile: CandidateProfile,
    settings: Settings,
) -> FilterResult:
    """Apply every hard rule; return the pass/fail verdict plus reasons.

    Every rule also contributes a :class:`FilterCheck` — what was compared,
    against what, or why the rule was skipped — so passing jobs remain auditable.
    """
    rejections: list[FilterRejection] = []
    checks: list[FilterCheck] = []

    # 1. Required experience exceeds what the candidate qualifies for.
    my_years = settings.candidate.my_years_experience
    allowed = my_years + settings.candidate.experience_tolerance
    if parsed.min_years_experience is not None:
        checks.append(
            _check(
                "too_much_experience",
                checked=True,
                value=f"min_years {parsed.min_years_experience:g}",
                limit=f"allowed {allowed:g} (my_years {my_years:g} + tolerance {settings.candidate.experience_tolerance:g})",
            )
        )
        if parsed.min_years_experience > allowed:
            rejections.append(
                FilterRejection(
                    rule_id="too_much_experience",
                    evidence=(
                        f"requires {parsed.min_years_experience:g} yr(s); allowed up to {allowed:g} "
                        f"[{(parsed.years_source_quote or 'no quote')}]"
                    ),
                )
            )
    else:
        checks.append(
            _check(
                "too_much_experience",
                checked=False,
                skipped_reason="min_years_experience not stated",
            )
        )

    # 2. Senior/lead/manager title.
    title_level = parsed.title_seniority
    raw_level = detect_title_seniority(job.title or "")
    if SENIORITY_RANK[raw_level] > SENIORITY_RANK[title_level]:
        title_level = raw_level
    checks.append(
        _check(
            "senior_title",
            checked=True,
            value=f"title seniority={title_level.value}",
            limit=f"disallowed: {sorted(level.value for level in DISALLOWED_SENIORITY)}",
        )
    )
    if title_level in DISALLOWED_SENIORITY:
        rejections.append(
            FilterRejection(rule_id="senior_title", evidence=f"title seniority={title_level.value}")
        )

    # 3. Location / remote preference.
    if parsed.remote_type == RemoteType.remote:
        checks.append(
            _check(
                "location_mismatch",
                checked=True,
                value=f"remote_type={parsed.remote_type.value}",
                limit=f"remote_ok={settings.targets.remote_ok}",
            )
        )
        if not settings.targets.remote_ok:
            rejections.append(
                FilterRejection(
                    rule_id="location_mismatch",
                    evidence="remote role but remote not allowed",
                )
            )
    elif not settings.targets.locations:
        checks.append(
            _check(
                "location_mismatch",
                checked=False,
                skipped_reason="no target locations set",
            )
        )
    else:
        haystack = f"{job.location or ''} {parsed.location or ''}".lower()
        matched = any(pref.lower() in haystack for pref in settings.targets.locations)
        checks.append(
            _check(
                "location_mismatch",
                checked=True,
                value=f"location '{job.location or parsed.location}'",
                limit=f"targets: {settings.targets.locations}",
            )
        )
        if not matched:
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
        checks.append(
            _check(
                "employment_type_mismatch",
                checked=True,
                value=f"employment_type={parsed.employment_type}",
                limit=f"targets: {settings.targets.employment_types}",
            )
        )
    elif parsed.employment_type is None:
        checks.append(
            _check(
                "employment_type_mismatch",
                checked=False,
                skipped_reason="employment type not stated",
            )
        )
    else:
        checks.append(
            _check(
                "employment_type_mismatch",
                checked=False,
                skipped_reason="no target employment types set",
            )
        )

    # 5. Blacklisted company.
    company = (job.company or "").strip().lower()
    checks.append(
        _check(
            "blacklisted_company",
            checked=True,
            value=f"company='{job.company or ''}'",
            limit=f"blacklist: {settings.companies.blacklist_companies}",
        )
    )
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
        checks.append(
            _check(
                "salary_below_floor",
                checked=True,
                value=f"salary='{parsed.salary}'",
                limit=f"floor {settings.targets.salary_floor}",
            )
        )
        if value is not None and value < settings.targets.salary_floor:
            rejections.append(
                FilterRejection(
                    rule_id="salary_below_floor",
                    evidence=f"'{parsed.salary}' below floor {settings.targets.salary_floor}",
                )
            )
    elif not settings.targets.salary_floor:
        checks.append(
            _check(
                "salary_below_floor",
                checked=False,
                skipped_reason="no salary floor set",
            )
        )
    else:
        checks.append(
            _check(
                "salary_below_floor",
                checked=False,
                skipped_reason="salary not stated",
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
        checks.append(
            _check(
                "deal_breaker_skill",
                checked=True,
                value=f"must_have_skills={parsed.must_have_skills}",
                limit=f"deal breakers: {settings.filters.deal_breaker_skills}",
            )
        )
    else:
        checks.append(
            _check(
                "deal_breaker_skill",
                checked=False,
                skipped_reason="no deal-breaker skills configured",
            )
        )

    # 8. Stale posting.
    if job.posted_at and settings.filters.max_job_age_days:
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        age_days = (datetime.now(UTC) - posted).days
        checks.append(
            _check(
                "stale_job",
                checked=True,
                value=f"age {age_days} days",
                limit=f"max_job_age_days {settings.filters.max_job_age_days}",
            )
        )
        if age_days > settings.filters.max_job_age_days:
            rejections.append(
                FilterRejection(rule_id="stale_job", evidence=f"posted {age_days} days ago")
            )
    elif job.posted_at is None:
        checks.append(_check("stale_job", checked=False, skipped_reason="posted date unknown"))
    else:
        checks.append(
            _check("stale_job", checked=False, skipped_reason="no max job age configured")
        )

    # 9. Non-job listing (talent-pool / general-application boilerplate).
    title_text = (job.title or "").lower()
    matched_phrase = next(
        (phrase for phrase in NON_JOB_LISTING_PHRASES if phrase in title_text), None
    )
    checks.append(
        _check(
            "non_job_listing",
            checked=True,
            value=f"title='{job.title or ''}'",
            limit=f"phrases: {list(NON_JOB_LISTING_PHRASES)}",
        )
    )
    if matched_phrase is not None:
        rejections.append(
            FilterRejection(
                rule_id="non_job_listing",
                evidence=f"title matches non-job listing phrase '{matched_phrase}'",
            )
        )

    return FilterResult(passed=not rejections, rejections=rejections, checks=checks)
