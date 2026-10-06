"""Central configuration loader.

Preferences live in ``config.yaml`` (a human edits it). Secrets live in
``.env`` (never committed). This module merges both into a single validated
:class:`Settings` object that the rest of the pipeline consumes.

Import ``get_settings()`` anywhere; the file is read once and cached.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


class CandidateConfig(BaseModel):
    my_years_experience: float = 0
    experience_tolerance: float = 1


class TargetsConfig(BaseModel):
    target_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    employment_types: list[str] = Field(default_factory=lambda: ["full-time", "internship"])
    salary_floor: float | None = None


class CompaniesConfig(BaseModel):
    target_companies: list[str] = Field(default_factory=list)
    blacklist_companies: list[str] = Field(default_factory=list)


class FiltersConfig(BaseModel):
    deal_breaker_skills: list[str] = Field(default_factory=list)
    max_job_age_days: int = 14
    must_have_coverage_floor: float = 0.6
    min_parse_confidence: float = 0.5
    min_score_to_send: float = 60
    top_k: int = 10


class ModelsConfig(BaseModel):
    extractor: str = "openai/gpt-oss-120b"
    evaluator: str = "openai/gpt-oss-120b"
    verifier: str = "qwen/qwen3.8-27b"
    temperature: float = 0.0


class SourcesConfig(BaseModel):
    """Which collectors run, and their parameters (plan section 5.3)."""

    remoteok: bool = True
    remotive: bool = True
    adzuna: bool = True
    greenhouse: bool = True
    lever: bool = True
    ashby: bool = True
    #: Opt-in: attribution/link-back is required by the provider's API terms.
    himalayas: bool = False
    #: Opt-in: the API asks for a link back to arbeitnow.com.
    arbeitnow: bool = False
    #: Opt-in: the API's friendlyNotice requires crediting Jobicy as the source.
    jobicy: bool = False
    #: Opt-in: RSS is permitted with attribution, but the API terms forbid
    #: scraping/storing data. Owner must confirm before enabling.
    weworkremotely: bool = False
    #: Opt-in: the API is public but api.smartrecruiters.com/robots.txt disallows
    #: all crawlers. Owner must confirm the conflict before enabling.
    smartrecruiters: bool = False
    #: Opt-in: robots.txt allows crawling, but no clear third-party feed licence
    #: was found in Workable's public terms.
    workable: bool = False
    adzuna_country: str = "in"
    results_per_page: int = 50


class RankingWeights(BaseModel):
    fit_score: float = 0.50
    must_have_coverage: float = 0.30
    seniority_fit: float = 0.15
    preference_bonus: float = 0.05


class RankingConfig(BaseModel):
    weights: RankingWeights = Field(default_factory=RankingWeights)


class DecisionWeights(BaseModel):
    """Weights for the decision-engine dimensions. Must sum to 1.0."""

    skills_core: float = 0.30
    seniority_fit: float = 0.20
    role_fit: float = 0.15
    project_relevance: float = 0.15
    skills_secondary: float = 0.05
    education_fit: float = 0.05
    growth_fit: float = 0.05
    logistics_fit: float = 0.05

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> DecisionWeights:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"decision weights must sum to 1.0 (got {total:.4f})")
        return self


class DecisionConfig(BaseModel):
    """Thresholds and floors for the deterministic decision rule (section 4)."""

    weights: DecisionWeights = Field(default_factory=DecisionWeights)
    apply_threshold: float = 80.0
    review_threshold: float = 65.0
    c_min: float = 0.5
    c_apply: float = 0.75
    seniority_floor: float = 50.0
    coverage_floor: float = 0.6
    # Risk flags each subtract this many points, capped at ``max_risk_penalty``.
    risk_penalty_per_flag: float = 10.0
    max_risk_penalty: float = 40.0
    # LLM-scored dimensions are sampled this many times; spread above
    # ``spread_limit`` lowers confidence.
    samples: int = 3
    spread_limit: float = 15.0
    margin_scale: float = 20.0


class ScheduleConfig(BaseModel):
    daily_hour: int = 9


class Secrets(BaseModel):
    """Values sourced from the environment / .env. Never written to disk."""

    groq_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    resend_api_key: str = ""
    resend_from_email: str = ""
    resend_to_email: str = ""


class Settings(BaseModel):
    candidate: CandidateConfig = Field(default_factory=CandidateConfig)
    targets: TargetsConfig = Field(default_factory=TargetsConfig)
    companies: CompaniesConfig = Field(default_factory=CompaniesConfig)
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    ranking: RankingConfig = Field(default_factory=RankingConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    secrets: Secrets = Field(default_factory=Secrets)


def _load_secrets() -> Secrets:
    return Secrets(
        groq_api_key=os.environ.get("GROQ_API_KEY", ""),
        adzuna_app_id=os.environ.get("ADZUNA_APP_ID", ""),
        adzuna_app_key=os.environ.get("ADZUNA_APP_KEY", ""),
        resend_api_key=os.environ.get("RESEND_API_KEY", ""),
        resend_from_email=os.environ.get("RESEND_FROM_EMAIL", ""),
        resend_to_email=os.environ.get("RESEND_TO_EMAIL", ""),
    )


def load_settings(config_path: Path | str = CONFIG_PATH) -> Settings:
    """Parse config.yaml + .env into a validated Settings object."""
    load_dotenv(REPO_ROOT / ".env", override=False)

    config_path = Path(config_path)
    raw: dict = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

    return Settings(**raw, secrets=_load_secrets())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor used everywhere in the pipeline."""
    return load_settings()


def config_hash() -> str:
    """Short fingerprint of the behaviour-defining config files."""
    digest = hashlib.sha256()
    for name in ("config.yaml", "skills.yaml"):
        path = REPO_ROOT / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
