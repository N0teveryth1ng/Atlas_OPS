from datetime import UTC, datetime, timedelta

import pytest

from atlas.config import Settings, settings_for_profile
from atlas.filters import NON_JOB_LISTING_PHRASES, _parse_salary, apply_hard_filters
from atlas.schemas import CandidateProfile, ExperienceLevel, Job, ParsedJD, RemoteType, Seniority


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
