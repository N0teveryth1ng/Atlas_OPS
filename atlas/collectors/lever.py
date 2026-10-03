"""Lever public job-board collector (per target company)."""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, parse_datetime


class LeverCollector(Collector):
    name = "lever"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json)
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _board(self, token: str) -> list[Job]:
        if token in self._cache:
            return self._cache[token]
        url = f"https://api.lever.co/v0/postings/{token}"
        payload = self.get_json(url, {"mode": "json"})
        jobs: list[Job] = []
        for item in payload or []:
            link = item.get("hostedUrl") or ""
            if not link:
                continue
            categories = item.get("categories") or {}
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") else None,
                    title=item.get("text"),
                    company=token,
                    location=categories.get("location"),
                    url=link,
                    description_raw=item.get("descriptionPlain") or item.get("description") or "",
                    posted_at=parse_datetime(item.get("createdAt")),
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
