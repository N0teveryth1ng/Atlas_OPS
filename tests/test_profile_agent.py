from atlas.profile_agent import (
    _extraction_to_profile,
    derive_experience_level,
    merge_profiles,
    normalize_skill_name,
)
from atlas.schemas import (
    CandidateProfile,
    ExperienceLevel,
    InputMode,
    Proficiency,
    ProfileExtraction,
    RawSkill,
    Skill,
)


def test_normalize_skill_name():
    assert normalize_skill_name("  React   JS ") == "react js"


def test_derive_experience_level_thresholds():
    assert derive_experience_level(0) == ExperienceLevel.fresher
    assert derive_experience_level(11) == ExperienceLevel.fresher
    assert derive_experience_level(12) == ExperienceLevel.junior
    assert derive_experience_level(36) == ExperienceLevel.mid
    assert derive_experience_level(120) == ExperienceLevel.senior


def test_description_caps_proficiency_and_sets_canonical_name():
    extraction = ProfileExtraction(skills=[RawSkill(name="Python", proficiency=Proficiency.strong)])
    profile = _extraction_to_profile(extraction, InputMode.description, cap_proficiency=True)
    assert profile.skills[0].proficiency == Proficiency.familiar
    assert profile.skills[0].canonical_name == "python"


def test_description_without_experience_flags_ambiguity():
    profile = _extraction_to_profile(ProfileExtraction(), InputMode.description)
    assert "experience_level_unknown" in profile.ambiguities
    assert profile.experience_level == ExperienceLevel.fresher


def test_merge_description_overrides_roles_resume_wins_history():
    resume = CandidateProfile(
        source_mode=InputMode.resume,
        full_name="A B",
        total_experience_months=0,
        experience_level=ExperienceLevel.fresher,
        target_roles=["Backend Developer"],
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.working)],
    )
    description = CandidateProfile(
        source_mode=InputMode.description,
        target_roles=["ML Engineer"],
        skills=[Skill(name="SQL", canonical_name="sql")],
        certifications=["AWS Certified"],
    )

    merged = merge_profiles(resume, description)

    assert merged.source_mode == InputMode.combined
    # Preferences: description wins.
    assert merged.target_roles == ["ML Engineer"]
    # Factual history: resume wins.
    assert merged.skills[0].name == "Python"
    assert merged.full_name == "A B"
    # Certifications union.
    assert "AWS Certified" in merged.certifications
