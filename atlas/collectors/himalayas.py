"""Himalayas remote-jobs API collector.

The public search endpoint (https://himalayas.app/api) accepts ``q``, ``country``
(ISO 3166-1 alpha-2), ``seniority``, ``employment_type``, ``sort`` and ``page``.
The ``limit`` parameter is for the browse endpoint only. Jobs returned may carry
multiple ``seniority`` values; we still request ``seniority=Entry-level`` (and
also an ``Intern`` pass). The API does *not* support broad text search filtering
server-side in a way that matches every case — our own filters decide, so we do
not apply a local needle filter.
"""

from __future__ import annotations

from typing import Any

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_URL = "https://himalayas.app/jobs/api/search"
PAGE_SIZE = 20  # API default/page size
SENIORITY = "Entry-level"
LOCATION_SEPARATOR = " / "
ANYWHERE_TOKENS = {"anywhere", "remote", "worldwide", "global"}


def _location(item: dict) -> str | None:
    """Map ``locationRestrictions`` into ``Job.location``.

    An empty list means no restriction; return ``None`` (do not invent).
    """
    restrictions = item.get("locationRestrictions")
    if not isinstance(restrictions, (list, tuple)):
        return None
    parts = [str(part).strip() for part in restrictions if str(part).strip()]
    if not parts:
        return None
    return LOCATION_SEPARATOR.join(dict.fromkeys(parts))


def _country_code(loc: str | None) -> str | None:
    """Return a 2-letter ISO country code if the location looks like a country.

    Keep it conservative: only return when the token is a single token that is
    not in ``ANYWHERE_TOKENS``. (We don't have a full ISO lookup here; callers
    may pass explicit codes in practice, but this is a best-effort.)
    """
    if not loc:
        return None
    token = loc.strip()
    if not token or token.lower() in ANYWHERE_TOKENS:
        return None
    # If it contains space/comma, not a single country code; skip.
    if " " in token or "," in token or "-" in token:
        return None
    if len(token) == 2:
        return token.upper()
    return None


class HimalayasCollector(Collector):
    name = "himalayas"
    query_based = True

    def __init__(self, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())

    def _fetch_page(self, params: dict[str, Any]) -> list[dict]:
        payload = self.get_json(API_URL, params) or {}
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            return jobs
        return []

    def _job_from_item(self, item: dict) -> Job | None:
        link = item.get("applicationLink") or item.get("guid") or ""
        if not link:
            return None
        extra: dict = {}
        if item.get("isRemote") or item.get("remoteRestriction") or item.get("isWorkFromHome"):
            extra["remote_type"] = RemoteType.remote
        return Job(
            source=self.name,
            source_id=str(item.get("id") or item.get("guid") or "") or None,
            title=item.get("title"),
            company=item.get("companyName"),
            location=_location(item),
            url=link,
            description_raw=item.get("description") or "",
            posted_at=parse_datetime(item.get("pubDate")),
            **extra,
        )

    def _fetch_for_params(self, base_params: dict[str, Any], max_pages: int = 3) -> list[Job]:
        seen: set[str] = set()
        out: list[Job] = []
        for page in range(1, max_pages + 1):
            params = dict(base_params)
            params["page"] = page
            items = self._fetch_page(params)
            if not items:
                break
            for item in items:
                job = self._job_from_item(item)
                if job is None:
                    continue
                key = job.url or (job.source_id or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                out.append(job)
        return out

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        base: dict[str, Any] = {"sort": "recent"}
        if needle:
            base["q"] = query
        # Try to infer country from profile context? Collector receives only query.
        # The caller (sourcing) uses profile to build queries; country omitted unless
        # the query/planner passes it. We keep it simple and omit if unknown.
        jobs: list[Job] = []
        # Pass 1: seniority Entry-level
        p1 = dict(base)
        p1["seniority"] = SENIORITY
        jobs.extend(self._fetch_for_params(p1, max_pages=3))
        # Pass 2: interns (employment_type=Intern)
        p2 = dict(base)
        p2["employment_type"] = "Intern"
        jobs.extend(self._fetch_for_params(p2, max_pages=3))
        # Deduplicate
        seen: set[str] = set()
        deduped: list[Job] = []
        for job in jobs:
            key = job.url or (job.source_id or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            deduped.append(job)
        return deduped
