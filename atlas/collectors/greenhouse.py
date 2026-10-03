"""Greenhouse public job-board collector (per target company)."""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, parse_datetime


class GreenhouseCollector(Collector):
    name = "greenhouse"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json)
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _board(self, token: str) -> list[Job]:
        if token in self._cache:
            return self._cache[token]
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        payload = self.get_json(url, {"content": "true"})
        jobs: list[Job] = []
        for item in (payload or {}).get("jobs", []):
            link = item.get("absolute_url") or ""
            if not link:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") is not None else None,
                    title=item.get("title"),
                    company=token,
                    location=(item.get("location") or {}).get("name"),
                    url=link,
                    description_raw=item.get("content") or "",
                    posted_at=parse_datetime(item.get("updated_at") or item.get("first_published")),
                )
            )
        self._cache[token] = jobs
        return jobs

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        out: list[Job] = []
        for token in self.companies:
            for job in self._board(token):
                if not needle or needle in (job.title or "").lower():
                    out.append(job.model_copy(deep=True))
        return out
