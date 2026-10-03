import pytest
from pydantic import ValidationError

from atlas.schemas import CandidateProfile, ExperienceLevel, Verdict


def test_verdict_score_bounds():
    with pytest.raises(ValidationError):
        Verdict(fit_score=150)
    with pytest.raises(ValidationError):
        Verdict(fit_score=-1)


def test_profile_defaults():
    profile = CandidateProfile()
    assert profile.experience_level == ExperienceLevel.fresher
    assert profile.approved is False
    assert profile.total_experience_months == 0


def test_missing_critical_fields_reported():
    profile = CandidateProfile()
    missing = set(profile.missing_critical_fields())
    assert {"skills", "target_roles", "email"} <= missing
