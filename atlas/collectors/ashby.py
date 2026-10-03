"""Ashby public job-board collector (per target company)."""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, parse_datetime


class AshbyCollector(Collector):
    name = "ashby"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json)
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _board(self, token: str) -> list[Job]:
        if token in self._cache:
            return self._cache[token]
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
        payload = self.get_json(url, {"includeCompensation": "true"})
        jobs: list[Job] = []
        for item in (payload or {}).get("jobs", []):
            link = item.get("jobUrl") or item.get("applyUrl") or ""
            if not link:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") else None,
                    title=item.get("title"),
                    company=token,
                    location=item.get("location"),
                    url=link,
                    description_raw=item.get("descriptionPlain")
                    or item.get("descriptionHtml")
                    or "",
                    posted_at=parse_datetime(item.get("publishedAt")),
                )
            )
        self._cache[token] = jobs
        return jobs

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        return [
            job.model_copy(deep=True)
            for token in self.companies
            for job in self._board(token)
            if not needle or needle in (job.title or "").lower()
        ]
