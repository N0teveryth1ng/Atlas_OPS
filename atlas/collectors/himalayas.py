"""Himalayas remote-jobs API collector.

Public JSON API (https://himalayas.app/jobs/api), max 20 jobs per page. Returns a
single page per fetch; rate limiting answers 429 with Retry-After, which the
shared HttpFetcher honours.
"""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_URL = "https://himalayas.app/jobs/api/search"
PAGE_SIZE = 20


class HimalayasCollector(Collector):
    name = "himalayas"
    query_based = True

    def __init__(self, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        # The API filters server-side; the local pass below is a safety net for
        # sources that answer with an unfiltered page.
        params: dict = {"limit": PAGE_SIZE}
        if needle:
            params["query"] = needle
        payload = self.get_json(API_URL, params) or {}
        jobs: list[Job] = []
        for item in payload.get("jobs") or []:
            link = item.get("applicationLink") or item.get("guid") or ""
            if not link:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") else None,
                    title=item.get("title"),
                    company=item.get("companyName"),
                    location=item.get("location"),
                    url=link,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("pubDate")),
                )
            )
        if not needle:
            return jobs
        return [
            job.model_copy(deep=True)
            for job in jobs
            if needle
            in " ".join(filter(None, [job.title, job.company, job.description_raw])).lower()
        ]
