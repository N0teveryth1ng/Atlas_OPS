"""Workable public job-board widget collector (per target company).

The endpoint that actually works is the v1 widget API::

    https://apply.workable.com/api/v1/widget/accounts/{subdomain}?details=true

Not ``/api/v3/accounts/...`` — that path returns 404. Jobs arrive under ``jobs``
with ``shortcode`` as the stable id.

Caveat recorded in the fixture README: ``apply.workable.com/robots.txt`` permits
crawling and declares ``ai-input: yes, ai-train: no``, but no clear third-party
feed licence was found in Workable's public terms, so this source stays opt-in
pending owner confirmation.
"""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_ROOT = "https://apply.workable.com/api/v1/widget/accounts"


class WorkableCollector(Collector):
    name = "workable"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _board(self, company: str) -> list[Job]:
        if company in self._cache:
            return self._cache[company]
        url = f"{API_ROOT}/{company}"
        payload = self.get_json(url, {"details": "true"}) or {}
        jobs: list[Job] = []
        for item in payload.get("jobs") or []:
            link = item.get("url") or item.get("application_url") or ""
            if not link:
                continue
            first_location = (item.get("locations") or [{}])[0]
            parts = [
                first_location.get("city") or item.get("city"),
                first_location.get("region") or item.get("state"),
                first_location.get("country") or item.get("country"),
            ]
            location = ", ".join(part for part in parts if part) or None
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["shortcode"]) if item.get("shortcode") else None,
                    title=item.get("title"),
                    company=company,
                    location=location,
                    url=link,
                    description_raw=str(item.get("description") or ""),
                    posted_at=parse_datetime(item.get("created_at")),
                )
            )
        self._cache[company] = jobs
        return jobs

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        return [
            job.model_copy(deep=True)
            for company in self.companies
            for job in self._board(company)
            if not needle or needle in (job.title or "").lower()
        ]
