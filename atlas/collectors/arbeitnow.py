"""Arbeitnow job-board API collector.

Free public API (https://www.arbeitnow.com/api/job-board-api) wrapped in a
paginated envelope: jobs live under ``data``, not at the top level. Terms ask for
a link back to the site and no abusive use.

The API exposes no visa-sponsorship field. ``visa_sponsorship`` is kept as a
collector-owned attribute so a future field can be surfaced without touching the
:class:`~atlas.schemas.Job` model; today it is always ``None``.
"""

from __future__ import annotations

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowCollector(Collector):
    name = "arbeitnow"
    query_based = True

    def __init__(self, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())
        #: No visa-sponsorship data is published by this API; kept for symmetry
        #: with sources that do publish it. ``Job`` has no field for it.
        self.visa_sponsorship: bool | None = None

    def fetch(self, query: str) -> list[Job]:
        payload = self.get_json(API_URL, None) or {}
        jobs: list[Job] = []
        # Arbeitnow has no search parameter, so the whole page is fetched and
        # matched locally.
        for item in payload.get("data") or []:
            url = item.get("url") or ""
            if not url:
                continue
            # No visa_sponsorship key in the payload; read defensively anyway.
            self.visa_sponsorship = item.get("visa_sponsorship")
            extra: dict = {}
            if item.get("remote"):
                extra["remote_type"] = RemoteType.remote
            jobs.append(
                Job(
                    source=self.name,
                    source_id=item.get("slug"),
                    title=item.get("title"),
                    company=item.get("company_name"),
                    location=item.get("location"),
                    url=url,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("created_at")),
                    **extra,
                )
            )
        needle = (query or "").lower().strip()
        if not needle:
            return jobs
        return [
            job.model_copy(deep=True)
            for job in jobs
            if needle
            in " ".join(filter(None, [job.title, job.company, job.description_raw])).lower()
        ]
