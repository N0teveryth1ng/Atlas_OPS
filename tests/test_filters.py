import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from atlas.config import Settings, settings_for_profile
from atlas.filters import (
    DISALLOWED_SENIORITY,
    NON_JOB_LISTING_PHRASES,
    _check,
    _parse_salary,
    apply_cheap_filters,
    apply_hard_filters,
)
from atlas.jd_parser import SENIORITY_RANK, detect_title_seniority, parse_jd_regex_only
from atlas.schemas import (
    CandidateProfile,
    ExperienceLevel,
    FilterCheck,
    FilterRejection,
    FilterResult,
    Job,
    ParsedJD,
    RemoteType,
    Seniority,
)


def _settings() -> Settings:
    settings = Settings()
    settings.candidate.my_years_experience = 0
    settings.candidate.experience_tolerance = 1
    return settings


def _job(
    title: str = "Software Engineer", company: str = "Acme", location: str = "Remote", **kwargs
) -> Job:
    return Job(
        source="test", title=title, company=company, location=location, url="test://1", **kwargs
    )


def _profile() -> CandidateProfile:
    return CandidateProfile()


def test_fresher_junior_role_passes():
    result = apply_hard_filters(
        _job(title="Junior Python Developer"),
        ParsedJD(min_years_experience=1, title_seniority=Seniority.junior),
        _profile(),
        _settings(),
    )
    assert result.passed
    assert result.rejections == []


def test_too_much_experience_rejected():
    result = apply_hard_filters(
        _job(),
        ParsedJD(min_years_experience=3, years_source_quote="3+ years"),
        _profile(),
        _settings(),
    )
    assert not result.passed
    assert any(r.rule_id == "too_much_experience" for r in result.rejections)


def test_senior_title_rejected_even_without_years():
    result = apply_hard_filters(
        _job(title="Senior Software Engineer"),
        ParsedJD(title_seniority=Seniority.unknown),
        _profile(),
        _settings(),
    )
    assert any(r.rule_id == "senior_title" for r in result.rejections)


def test_blacklisted_company_rejected():
    settings = _settings()
    settings.companies.blacklist_companies = ["BadCorp"]
    result = apply_hard_filters(_job(company="BadCorp Inc"), ParsedJD(), _profile(), settings)
    assert any(r.rule_id == "blacklisted_company" for r in result.rejections)


def test_remote_not_allowed_rejected():
    settings = _settings()
    settings.targets.remote_ok = False
    result = apply_hard_filters(
        _job(), ParsedJD(remote_type=RemoteType.remote), _profile(), settings
    )
    assert any(r.rule_id == "location_mismatch" for r in result.rejections)


def test_deal_breaker_skill_rejected():
    settings = _settings()
    settings.filters.deal_breaker_skills = ["Kubernetes"]
    result = apply_hard_filters(
        _job(), ParsedJD(must_have_skills=["Docker", "Kubernetes"]), _profile(), settings
    )
    assert any(r.rule_id == "deal_breaker_skill" for r in result.rejections)


def test_salary_below_floor_rejected():
    settings = _settings()
    settings.targets.salary_floor = 500_000
    result = apply_hard_filters(_job(), ParsedJD(salary="3 LPA"), _profile(), settings)
    assert any(r.rule_id == "salary_below_floor" for r in result.rejections)


def test_salary_at_or_above_floor_passes():
    settings = _settings()
    settings.targets.salary_floor = 500_000
    result = apply_hard_filters(_job(), ParsedJD(salary="12 LPA"), _profile(), settings)
    assert result.passed


def test_parse_salary_units_and_errors():
    assert _parse_salary("") is None
    assert _parse_salary("competitive") is None
    assert _parse_salary("1.2.3") is None
    assert _parse_salary("60k") == 60_000
    assert _parse_salary("1.2m") == 1_200_000
    assert _parse_salary("8 lakhs") == 800_000
    assert _parse_salary("90000") == 90_000


def test_location_mismatch_when_preferred_locations_set():
    settings = _settings()
    settings.targets.locations = ["Bengaluru"]
    result = apply_hard_filters(
        _job(location="Berlin"),
        ParsedJD(location="Berlin", remote_type=RemoteType.onsite),
        _profile(),
        settings,
    )
    assert any(r.rule_id == "location_mismatch" for r in result.rejections)


def test_location_match_passes():
    settings = _settings()
    settings.targets.locations = ["Bengaluru"]
    result = apply_hard_filters(
        _job(location="Bengaluru, India"),
        ParsedJD(location="Bengaluru", remote_type=RemoteType.onsite),
        _profile(),
        settings,
    )
    assert result.passed


def test_employment_type_mismatch_rejected():
    settings = _settings()
    settings.targets.employment_types = ["full_time"]
    result = apply_hard_filters(
        _job(),
        ParsedJD(employment_type="internship"),
        _profile(),
        settings,
    )
    assert any(r.rule_id == "employment_type_mismatch" for r in result.rejections)


def test_stale_job_rejected():
    settings = _settings()
    settings.filters.max_job_age_days = 30
    old = datetime.now(UTC) - timedelta(days=90)
    result = apply_hard_filters(_job(posted_at=old), ParsedJD(), _profile(), settings)
    assert any(r.rule_id == "stale_job" for r in result.rejections)


def test_fresh_naive_job_passes_once_timestamped():
    settings = _settings()
    settings.filters.max_job_age_days = 30
    naive_recent = datetime.now(UTC).replace(tzinfo=None)
    result = apply_hard_filters(_job(posted_at=naive_recent), ParsedJD(), _profile(), settings)
    assert result.passed


def test_profile_locations_wire_into_filters():
    profile = CandidateProfile(locations=["India"])
    settings = settings_for_profile(profile, _settings())

    assert settings.targets.locations == ["India"]
    zurich = apply_hard_filters(
        _job(location="Zurich"),
        ParsedJD(location="Zurich", remote_type=RemoteType.onsite),
        profile,
        settings,
    )
    assert not zurich.passed
    assert any(r.rule_id == "location_mismatch" for r in zurich.rejections)


def test_profile_anywhere_clears_location_filter():
    profile = CandidateProfile(locations=["anywhere"])
    settings = settings_for_profile(profile, _settings())

    assert settings.targets.locations == []
    zurich = apply_hard_filters(
        _job(location="Zurich"),
        ParsedJD(location="Zurich", remote_type=RemoteType.onsite),
        profile,
        settings,
    )
    assert zurich.passed


def test_settings_for_profile_wires_roles_and_experience_and_copies():
    base = _settings()
    profile = CandidateProfile(target_roles=["Backend"], total_experience_months=18)

    settings = settings_for_profile(profile, base)

    assert settings.targets.target_roles == ["Backend"]
    assert settings.candidate.my_years_experience == 1.5
    # Purity: the base object the pipeline cached is untouched.
    assert base.targets.target_roles == []
    assert base.candidate.my_years_experience == 0


def test_settings_for_profile_falls_back_to_level_for_years():
    settings = settings_for_profile(
        CandidateProfile(experience_level=ExperienceLevel.junior), _settings()
    )
    assert settings.candidate.my_years_experience == 1.0


@pytest.mark.parametrize("phrase", NON_JOB_LISTING_PHRASES)
def test_each_non_job_listing_phrase_is_rejected(phrase):
    result = apply_hard_filters(
        _job(title=f"{phrase.title()} - Software Engineer"),
        ParsedJD(),
        _profile(),
        _settings(),
    )
    assert not result.passed
    assert any(r.rule_id == "non_job_listing" for r in result.rejections)


def test_talent_acquisition_specialist_is_not_a_non_job_listing():
    result = apply_hard_filters(
        _job(title="Talent Acquisition Specialist"), ParsedJD(), _profile(), _settings()
    )
    assert not any(r.rule_id == "non_job_listing" for r in result.rejections)


def test_junior_python_developer_is_not_a_non_job_listing():
    result = apply_hard_filters(
        _job(title="Junior Python Developer"), ParsedJD(), _profile(), _settings()
    )
    assert result.passed
    assert not any(r.rule_id == "non_job_listing" for r in result.rejections)


def test_title_with_no_phrase_passes_the_non_job_listing_rule():
    result = apply_hard_filters(_job(title="Backend Engineer"), ParsedJD(), _profile(), _settings())
    assert result.passed


def test_non_job_listing_records_the_matched_phrase_as_evidence():
    result = apply_hard_filters(
        _job(title="Talent Community - Software Engineer"),
        ParsedJD(),
        _profile(),
        _settings(),
    )
    rejection = next(r for r in result.rejections if r.rule_id == "non_job_listing")
    assert "talent community" in rejection.evidence


def test_cheap_filter_passes_a_clean_junior_remote_job():
    result = apply_cheap_filters(
        _job(
            title="Junior Python Developer",
            location="Remote",
            description_raw="Junior role. 0-2 years experience. Fully remote.",
        ),
        _settings(),
    )
    assert result.passed
    assert result.rejections == []


def test_cheap_filter_rejects_senior_title():
    result = apply_cheap_filters(_job(title="Senior Software Engineer"), _settings())
    assert not result.passed
    assert any(r.rule_id == "senior_title" for r in result.rejections)


def test_cheap_filter_rejects_too_much_experience_from_regex():
    result = apply_cheap_filters(
        _job(description_raw="We need 5+ years of experience."), _settings()
    )
    assert not result.passed
    assert any(r.rule_id == "too_much_experience" for r in result.rejections)


def test_cheap_filter_rejects_non_job_listing():
    result = apply_cheap_filters(_job(title="Talent Pool - Software Engineer"), _settings())
    assert not result.passed
    assert any(r.rule_id == "non_job_listing" for r in result.rejections)


def test_cheap_filter_rejects_stale_job():
    settings = _settings()
    settings.filters.max_job_age_days = 30
    old = datetime.now(UTC) - timedelta(days=90)
    result = apply_cheap_filters(_job(posted_at=old), settings)
    assert not result.passed
    assert any(r.rule_id == "stale_job" for r in result.rejections)


def test_cheap_filter_rejects_blacklisted_company():
    settings = _settings()
    settings.companies.blacklist_companies = ["BadCorp"]
    result = apply_cheap_filters(_job(company="BadCorp Inc"), settings)
    assert not result.passed
    assert any(r.rule_id == "blacklisted_company" for r in result.rejections)


def test_cheap_filter_rejects_remote_when_remote_not_allowed():
    settings = _settings()
    settings.targets.remote_ok = False
    result = apply_cheap_filters(
        _job(title="Backend Engineer", description_raw="This role is fully remote."), settings
    )
    assert not result.passed
    assert any(r.rule_id == "location_mismatch" for r in result.rejections)


def test_cheap_filter_defers_non_remote_location_to_llm():
    # The regex parse cannot see a remote type here, but the LLM parse could still
    # resolve the job to remote, so the cheap pass must not reject on location.
    settings = _settings()
    settings.targets.locations = ["Bengaluru"]
    result = apply_cheap_filters(_job(title="Backend Engineer", location="Berlin"), settings)
    assert result.passed


# --------------------------------------------------------------------------- #
# Differential test: the rule-helper refactor must not change behaviour.
#
# ``_reference_apply_hard_filters`` is the pre-refactor ``apply_hard_filters``,
# copied verbatim from ``origin/main`` (commit 8abeaa1). The new implementation is
# a loop over per-rule helpers; both must produce identical (passed, rule_id,
# evidence) results for every input.
# --------------------------------------------------------------------------- #


def _reference_apply_hard_filters(
    job: Job,
    parsed: ParsedJD,
    profile: CandidateProfile,
    settings: Settings,
) -> FilterResult:
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


def _diff_key(result: FilterResult) -> tuple:
    return (
        result.passed,
        sorted((r.rule_id, r.evidence) for r in result.rejections),
        [(c.rule_id, c.checked, c.value, c.limit, c.skipped_reason) for c in result.checks],
    )


def _golden_cases() -> list[tuple[Job, ParsedJD, CandidateProfile, Settings]]:
    path = Path(__file__).resolve().parents[1] / "eval" / "golden_set.jsonl"
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        job = Job(
            source="golden",
            title=row["title"],
            company=row.get("company"),
            location=row.get("location"),
            url=f"golden://{row['id']}",
        )
        parsed = parse_jd_regex_only(row["title"], row.get("description", ""))
        cases.append((job, parsed, CandidateProfile(), _settings()))
    return cases


def _case(
    *,
    title: str = "Software Engineer",
    company: str = "Acme",
    location: str = "Remote",
    parsed: ParsedJD | None = None,
    my_years: float = 0,
    tolerance: float = 1,
    remote_ok: bool = True,
    locations: list[str] | None = None,
    employment_types: list[str] | None = None,
    salary_floor: float | None = None,
    blacklist: list[str] | None = None,
    deal_breakers: list[str] | None = None,
    max_age: int = 14,
    posted_at: datetime | None = None,
) -> tuple[Job, ParsedJD, CandidateProfile, Settings]:
    settings = _settings()
    settings.candidate.my_years_experience = my_years
    settings.candidate.experience_tolerance = tolerance
    settings.targets.remote_ok = remote_ok
    if locations is not None:
        settings.targets.locations = locations
    if employment_types is not None:
        settings.targets.employment_types = employment_types
    settings.targets.salary_floor = salary_floor
    if blacklist is not None:
        settings.companies.blacklist_companies = blacklist
    if deal_breakers is not None:
        settings.filters.deal_breaker_skills = deal_breakers
    settings.filters.max_job_age_days = max_age
    job = _job(title=title, company=company, location=location, posted_at=posted_at)
    return (job, parsed if parsed is not None else ParsedJD(), _profile(), settings)


def _boundary_cases() -> list[tuple[Job, ParsedJD, CandidateProfile, Settings]]:
    junior, mid, senior, lead, principal, architect = (
        Seniority.junior,
        Seniority.mid,
        Seniority.senior,
        Seniority.lead,
        Seniority.principal,
        Seniority.architect,
    )
    recent = datetime.now(UTC)
    return [
        # too_much_experience (allowed = 0 + 1 = 1)
        _case(parsed=ParsedJD(min_years_experience=1, years_source_quote="1 yr")),
        _case(parsed=ParsedJD(min_years_experience=1.5, years_source_quote="18 mo")),
        _case(parsed=ParsedJD(min_years_experience=2, years_source_quote="2+")),
        _case(parsed=ParsedJD(min_years_experience=None)),
        # senior_title
        _case(parsed=ParsedJD(title_seniority=junior)),
        _case(parsed=ParsedJD(title_seniority=mid)),
        _case(parsed=ParsedJD(title_seniority=senior)),
        _case(parsed=ParsedJD(title_seniority=lead)),
        _case(parsed=ParsedJD(title_seniority=principal)),
        _case(parsed=ParsedJD(title_seniority=architect)),
        _case(title="Senior Software Engineer", parsed=ParsedJD(title_seniority=junior)),
        # location
        _case(parsed=ParsedJD(remote_type=RemoteType.remote), remote_ok=True),
        _case(parsed=ParsedJD(remote_type=RemoteType.remote), remote_ok=False),
        _case(
            parsed=ParsedJD(location="Bengaluru", remote_type=RemoteType.onsite),
            locations=["Bengaluru"],
        ),
        _case(
            parsed=ParsedJD(location="Berlin", remote_type=RemoteType.onsite),
            locations=["Bengaluru"],
        ),
        _case(parsed=ParsedJD(location="Berlin", remote_type=RemoteType.onsite), locations=[]),
        # employment_type
        _case(parsed=ParsedJD(employment_type="full-time"), employment_types=["full-time"]),
        _case(parsed=ParsedJD(employment_type="internship"), employment_types=["full-time"]),
        _case(parsed=ParsedJD(employment_type=None)),
        _case(parsed=ParsedJD(employment_type="contract"), employment_types=[]),
        # blacklist
        _case(company="BadCorp", blacklist=["BadCorp"]),
        _case(company="BadCorp Inc", blacklist=["badcorp"]),
        _case(company="GoodCorp", blacklist=["BadCorp"]),
        # salary
        _case(parsed=ParsedJD(salary="3 LPA"), salary_floor=500_000),
        _case(parsed=ParsedJD(salary="5 LPA"), salary_floor=500_000),
        _case(parsed=ParsedJD(salary="12 LPA"), salary_floor=500_000),
        _case(parsed=ParsedJD(salary="competitive"), salary_floor=500_000),
        # deal_breaker_skill
        _case(
            parsed=ParsedJD(must_have_skills=["Docker", "Kubernetes"]),
            deal_breakers=["Kubernetes"],
        ),
        _case(parsed=ParsedJD(must_have_skills=["Python"]), deal_breakers=["Kubernetes"]),
        # stale_job
        _case(posted_at=recent - timedelta(days=90), max_age=30),
        _case(posted_at=recent - timedelta(days=30), max_age=30),
        _case(posted_at=recent.replace(tzinfo=None), max_age=30),
        _case(posted_at=None, max_age=30),
        # non_job_listing
        _case(title="Talent Community - Software Engineer"),
        _case(title="Talent Acquisition Specialist"),
    ]


def test_refactor_preserves_behaviour_on_golden_set_and_boundaries():
    golden = _golden_cases()
    boundaries = _boundary_cases()
    assert len(golden) == 20
    assert len(boundaries) >= 30

    mismatches = []
    for job, parsed, profile, settings in golden + boundaries:
        reference = _reference_apply_hard_filters(job, parsed, profile, settings)
        current = apply_hard_filters(job, parsed, profile, settings)
        if _diff_key(reference) != _diff_key(current):
            mismatches.append((job.title, _diff_key(reference), _diff_key(current)))
    assert mismatches == []
