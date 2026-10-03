from datetime import datetime, timedelta, timezone

from atlas.config import Settings
from atlas.filters import _parse_salary, apply_hard_filters
from atlas.schemas import CandidateProfile, Job, ParsedJD, RemoteType, Seniority


def _settings() -> Settings:
    settings = Settings()
    settings.candidate.my_years_experience = 0
    settings.candidate.experience_tolerance = 1
    return settings


def _job(title: str = "Software Engineer", company: str = "Acme", location: str = "Remote", **kwargs) -> Job:
    return Job(source="test", title=title, company=company, location=location, url="test://1", **kwargs)


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
    result = apply_hard_filters(_job(), ParsedJD(remote_type=RemoteType.remote), _profile(), settings)
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
    old = datetime.now(timezone.utc) - timedelta(days=90)
    result = apply_hard_filters(_job(posted_at=old), ParsedJD(), _profile(), settings)
    assert any(r.rule_id == "stale_job" for r in result.rejections)


def test_fresh_naive_job_passes_once_timestamped():
    settings = _settings()
    settings.filters.max_job_age_days = 30
    naive_recent = datetime.now(timezone.utc).replace(tzinfo=None)
    result = apply_hard_filters(_job(posted_at=naive_recent), ParsedJD(), _profile(), settings)
    assert result.passed
