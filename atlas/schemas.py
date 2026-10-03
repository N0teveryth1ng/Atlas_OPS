"""Typed data models for the Atlas_OPS pipeline.

Everything that crosses a stage boundary is one of these models. LLM output is
never consumed as raw text: the model must validate into a schema, or the call
is retried.

Two families exist:

* ``*Extraction`` models — exactly what the LLM is asked to return. They are
  deliberately lenient (few required fields) so a mostly-correct response can
  still validate, with gaps recorded in ``ambiguities``.
* Canonical models — ``CandidateProfile``, ``ParsedJD``, ``Job``, ``Verdict`` —
  enriched by deterministic code (derived experience level, canonical skill
  names, regex-confirmed years, ...).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Proficiency(str, Enum):
    strong = "strong"
    working = "working"
    familiar = "familiar"


class SkillCategory(str, Enum):
    language = "language"
    framework = "framework"
    library = "library"
    tool = "tool"
    cloud = "cloud"
    database = "database"
    concept = "concept"
    other = "other"


class ExperienceLevel(str, Enum):
    fresher = "fresher"
    junior = "junior"
    mid = "mid"
    senior = "senior"


class InputMode(str, Enum):
    resume = "resume"
    description = "description"
    combined = "combined"


class Seniority(str, Enum):
    intern = "intern"
    entry = "entry"
    junior = "junior"
    mid = "mid"
    senior = "senior"
    lead = "lead"
    principal = "principal"
    manager = "manager"
    architect = "architect"
    unknown = "unknown"


class RemoteType(str, Enum):
    onsite = "onsite"
    hybrid = "hybrid"
    remote = "remote"
    unknown = "unknown"


class Recommendation(str, Enum):
    strong_apply = "strong_apply"
    apply = "apply"
    maybe = "maybe"
    skip = "skip"


# --------------------------------------------------------------------------- #
# Shared building blocks
# --------------------------------------------------------------------------- #


class Links(BaseModel):
    github: str | None = None
    linkedin: str | None = None
    portfolio: str | None = None


class Education(BaseModel):
    degree: str | None = None
    field: str | None = None
    institution: str | None = None
    graduation_year: int | None = None


class Experience(BaseModel):
    role: str | None = None
    company: str | None = None
    duration_months: int | None = None
    tech: list[str] = Field(default_factory=list)


class Project(BaseModel):
    title: str | None = None
    tech: list[str] = Field(default_factory=list)
    summary: str | None = None
    outcomes: str | None = None


class RawSkill(BaseModel):
    """Skill as returned by the LLM, before canonicalisation."""

    name: str
    category: SkillCategory = SkillCategory.other
    proficiency: Proficiency = Proficiency.familiar
    evidence: str | None = None
    last_used: str | None = None


class Skill(BaseModel):
    """Skill after canonicalisation against the skill ontology."""

    name: str
    canonical_name: str
    category: SkillCategory = SkillCategory.other
    proficiency: Proficiency = Proficiency.familiar
    evidence: str | None = None
    last_used: str | None = None


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


class ProfileExtraction(BaseModel):
    """Raw, lenient shape the LLM returns for any input mode."""

    full_name: str | None = None
    email: str | None = None
    links: Links = Field(default_factory=Links)
    total_experience_months: int | None = None
    experience_level_stated: ExperienceLevel | None = None
    education: list[Education] = Field(default_factory=list)
    skills: list[RawSkill] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    internships: list[Experience] = Field(default_factory=list)
    work: list[Experience] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)


class CandidateProfile(BaseModel):
    """Canonical, human-reviewed profile. Source of truth for the pipeline."""

    version: int = 1
    source_mode: InputMode = InputMode.resume
    full_name: str | None = None
    email: str | None = None
    links: Links = Field(default_factory=Links)
    total_experience_months: int = 0
    experience_level: ExperienceLevel = ExperienceLevel.fresher
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    internships: list[Experience] = Field(default_factory=list)
    work: list[Experience] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    # Fields that could not be determined and need human confirmation.
    ambiguities: list[str] = Field(default_factory=list)
    # Set once the human has reviewed and approved profile.json.
    approved: bool = False

    def missing_critical_fields(self) -> list[str]:
        """Fields the pipeline cannot run without (surfaced as questions)."""
        missing: list[str] = []
        if self.total_experience_months <= 0 and self.experience_level == ExperienceLevel.fresher:
            # A fresher with zero months is valid, but an *unset* value must be
            # confirmed. The profile builder flags this via ambiguities.
            pass
        if not self.skills:
            missing.append("skills")
        if not self.target_roles:
            missing.append("target_roles")
        if not self.email:
            missing.append("email")
        return missing


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


class Job(BaseModel):
    """Canonical job record stored in SQLite."""

    source: str
    source_id: str | None = None
    title: str | None = None
    company: str | None = None
    location: str | None = None
    remote_type: RemoteType = RemoteType.unknown
    url: str
    urls: list[str] = Field(default_factory=list)
    description_raw: str = ""
    posted_at: datetime | None = None
    fetched_at: datetime | None = None
    dedupe_key: str | None = None


class ParsedJD(BaseModel):
    """Structured job requirements (LLM output + regex pre-pass)."""

    title: str | None = None
    title_seniority: Seniority = Seniority.unknown
    min_years_experience: float | None = None
    max_years_experience: float | None = None
    years_source_quote: str | None = None
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    education_required: str | None = None
    employment_type: str | None = None
    location: str | None = None
    remote_type: RemoteType = RemoteType.unknown
    visa_relocation: str | None = None
    salary: str | None = None
    responsibilities_summary: str | None = None
    red_flags: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    ambiguities: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


class Verdict(BaseModel):
    """LLM evaluator output for a surviving job."""

    fit_score: float = Field(ge=0, le=100)
    skills_fit: float = Field(default=0, ge=0, le=100)
    seniority_fit: float = Field(default=0, ge=0, le=100)
    role_fit: float = Field(default=0, ge=0, le=100)
    growth_fit: float = Field(default=0, ge=0, le=100)
    company_signal: float = Field(default=0, ge=0, le=100)
    recommendation: Recommendation = Recommendation.maybe
    reasons_for: list[str] = Field(default_factory=list)
    reasons_against: list[str] = Field(default_factory=list)
    seniority_assessment: str | None = None
    uncertainties: list[str] = Field(default_factory=list)


class VerifierVerdict(BaseModel):
    """Adversarial second-pass output; may veto the evaluator."""

    veto: bool = False
    downgrade_to: Recommendation | None = None
    reasons_against: list[str] = Field(default_factory=list)
    hidden_seniority_signals: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Hard filter
# --------------------------------------------------------------------------- #


class FilterRejection(BaseModel):
    rule_id: str
    evidence: str


class FilterResult(BaseModel):
    passed: bool
    rejections: list[FilterRejection] = Field(default_factory=list)
