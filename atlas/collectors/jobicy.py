"""Jobicy remote-jobs API collector.

Public API at https://jobicy.com/api/v2/remote-jobs. No key required, and the
site publishes an AI catalog / MCP server, so automated reads are expected.

The API's ``friendlyNotice`` requires crediting Jobicy as the source and linking
to the original application URL, so the listing ``url`` is kept on the Job
alongside whatever direct apply link the item carries.
"""

from __future__ import annotations

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_URL = "https://jobicy.com/api/v2/remote-jobs"
PAGE_SIZE = 50


class JobicyCollector(Collector):
    name = "jobicy"
    query_based = True

    def __init__(self, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        params: dict = {"count": PAGE_SIZE}
        if needle:
            params["q"] = needle
        payload = self.get_json(API_URL, params) or {}
        jobs: list[Job] = []
        for item in payload.get("jobs") or []:
            url = item.get("url") or ""
            if not url:
                continue
            extra: dict = {}
            if item.get("remote"):
                extra["remote_type"] = RemoteType.remote
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]) if item.get("id") else None,
                    title=item.get("title"),
                    company=item.get("companyName"),
                    location=item.get("location"),
                    url=url,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("pubDate")),
                    **extra,
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
