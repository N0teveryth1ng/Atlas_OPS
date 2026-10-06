"""Collector registry / factory (plan section 5.3)."""

from __future__ import annotations

from ..companies import load_companies
from ..config import Settings
from .adzuna import AdzunaCollector
from .arbeitnow import ArbeitnowCollector
from .ashby import AshbyCollector
from .base import Collector, GetJson, default_get_json, parse_datetime
from .feeds import RemoteOKCollector, RemotiveCollector
from .greenhouse import GreenhouseCollector
from .himalayas import HimalayasCollector
from .jobicy import JobicyCollector
from .lever import LeverCollector
from .smartrecruiters import SmartRecruitersCollector
from .weworkremotely import WeWorkRemotelyCollector
from .workable import WorkableCollector

__all__ = [
    "AdzunaCollector",
    "ArbeitnowCollector",
    "AshbyCollector",
    "Collector",
    "GetJson",
    "GreenhouseCollector",
    "HimalayasCollector",
    "JobicyCollector",
    "LeverCollector",
    "RemoteOKCollector",
    "RemotiveCollector",
    "SmartRecruitersCollector",
    "WeWorkRemotelyCollector",
    "WorkableCollector",
    "build_collectors",
    "default_get_json",
    "parse_datetime",
]


def build_collectors(
    settings: Settings,
    *,
    get_json: GetJson | None = None,
    companies: dict[str, list[str]] | None = None,
) -> list[Collector]:
    """Instantiate every enabled collector, skipping sources with no targets."""
    sources = settings.sources
    companies = companies if companies is not None else load_companies()
    collectors: list[Collector] = []

    if sources.remoteok:
        collectors.append(RemoteOKCollector(get_json=get_json))
    if sources.remotive:
        collectors.append(RemotiveCollector(get_json=get_json))
    if sources.adzuna:
        collectors.append(
            AdzunaCollector(
                settings.secrets.adzuna_app_id,
                settings.secrets.adzuna_app_key,
                country=sources.adzuna_country,
                results_per_page=sources.results_per_page,
                get_json=get_json,
            )
        )
    if sources.greenhouse and companies.get("greenhouse"):
        collectors.append(GreenhouseCollector(companies["greenhouse"], get_json=get_json))
    if sources.lever and companies.get("lever"):
        collectors.append(LeverCollector(companies["lever"], get_json=get_json))
    if sources.ashby and companies.get("ashby"):
        collectors.append(AshbyCollector(companies["ashby"], get_json=get_json))
    if sources.himalayas:
        collectors.append(HimalayasCollector(get_json=get_json))
    if sources.arbeitnow:
        collectors.append(ArbeitnowCollector(get_json=get_json))
    if sources.jobicy:
        collectors.append(JobicyCollector(get_json=get_json))
    if sources.weworkremotely:
        # Feed is XML, so it uses its own text path and is not get_json-injectable.
        collectors.append(WeWorkRemotelyCollector())
    if sources.smartrecruiters and companies.get("smartrecruiters"):
        collectors.append(SmartRecruitersCollector(companies["smartrecruiters"], get_json=get_json))
    if sources.workable and companies.get("workable"):
        collectors.append(WorkableCollector(companies["workable"], get_json=get_json))
    return collectors
