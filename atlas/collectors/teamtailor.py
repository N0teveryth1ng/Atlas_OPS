"""Teamtailor public job-board feed collector (per target subdomain).

Each careers site serves a public JSON Feed at::

    https://{subdomain}.teamtailor.com/jobs.json

The document is a JSON Feed: ``items`` carries the feed-reader fields
(``title``/``url``/``date_published``) plus an embedded ``_jobposting`` object
with the real fields. ``_jobposting`` is preferred when present so the feed's
own truncation of fields does not matter, with the top-level keys as fallback.

The official ``api.teamtailor.com/v1/jobs`` endpoint requires an API key and is
deliberately not used. No third-party licence for the public ``jobs.json`` feed
was verified, so this source stays opt-in pending owner confirmation.
"""

from __future__ import annotations

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, HttpFetcher, parse_datetime

FEED_URL = "https://{subdomain}.teamtailor.com/jobs.json"


class TeamtailorCollector(Collector):
    name = "teamtailor"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _board(self, subdomain: str) -> list[Job]:
        if subdomain in self._cache:
            return self._cache[subdomain]
        url = FEED_URL.format(subdomain=subdomain)
        payload = self.get_json(url, None) or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        jobs: list[Job] = []
        for item in items or []:
            posting = item.get("_jobposting") if isinstance(item, dict) else None
            posting = posting if isinstance(posting, dict) else {}
            link = posting.get("careers_url") or item.get("url") or ""
            if not link:
                continue
            extra: dict = {}
            if posting.get("remote"):
                extra["remote_type"] = RemoteType.remote
            description = "\n\n".join(
                str(posting[key]) for key in ("description", "requirements") if posting.get(key)
            )
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(posting.get("id") or item.get("id") or "") or None,
                    title=posting.get("title") or item.get("title"),
                    company=posting.get("company_name") or subdomain,
                    location=posting.get("location"),
                    url=link,
                    description_raw=description,
                    posted_at=parse_datetime(
                        posting.get("created_at") or item.get("date_published")
                    ),
                    **extra,
                )
            )
        self._cache[subdomain] = jobs
        return jobs

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        return [
            job.model_copy(deep=True)
            for subdomain in self.companies
            for job in self._board(subdomain)
            if not needle or needle in (job.title or "").lower()
        ]
