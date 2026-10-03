"""Central configuration loader.

Preferences live in ``config.yaml`` (a human edits it). Secrets live in
``.env`` (never committed). This module merges both into a single validated
:class:`Settings` object that the rest of the pipeline consumes.

Import ``get_settings()`` anywhere; the file is read once and cached.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

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
    min_score_to_send: float = 60
    top_k: int = 10


class ModelsConfig(BaseModel):
    extractor: str = "llama-3.3-70b-versatile"
    evaluator: str = "llama-3.3-70b-versatile"
    verifier: str = "llama-3.1-8b-instant"
    temperature: float = 0.0


class RankingWeights(BaseModel):
    fit_score: float = 0.50
    must_have_coverage: float = 0.30
    seniority_fit: float = 0.15
    preference_bonus: float = 0.05


class RankingConfig(BaseModel):
    weights: RankingWeights = Field(default_factory=RankingWeights)


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
    ranking: RankingConfig = Field(default_factory=RankingConfig)
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
