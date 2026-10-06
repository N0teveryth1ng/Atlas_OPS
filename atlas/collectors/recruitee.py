"""Recruitee careers-site API collector (per target tenant).

Documented at https://docs.recruitee.com/reference/intro-to-careers-site-api.
Jobs arrive under an ``offers`` key rather than as a bare list, and each tenant
lives on its own subdomain::

    https://{tenant}.recruitee.com/api/offers

``created_at`` is not ISO-8601 — the feed publishes ``2026-09-25 10:30:00`` with
no timezone marker, and some tenants use ``25-09-2026``. Both are handled here
and read as UTC rather than being handed to ``fromisoformat`` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_ROOT = "https://{tenant}.recruitee.com/api/offers"

#: Formats seen in the wild, tried in order after ISO-8601 fails.
CUSTOM_DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d")


def parse_recruitee_date(value: object) -> datetime | None:
    """Parse Recruitee's custom date strings as UTC."""
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in CUSTOM_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


class RecruiteeCollector(Collector):
    name = "recruitee"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _board(self, company: str) -> list[Job]:
        if company in self._cache:
            return self._cache[company]
        url = API_ROOT.format(tenant=company)
        payload = self.get_json(url, None) or {}
        offers = payload.get("offers") if isinstance(payload, dict) else None
        jobs: list[Job] = []
        for item in offers or []:
            link = item.get("careers_url") or ""
            if not link:
                continue
            extra: dict = {}
            if item.get("remote"):
                extra["remote_type"] = RemoteType.remote
            description = "\n\n".join(
                str(item[key])
                for key in ("description", "requirements", "benefits")
                if item.get(key)
            )
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]) if item.get("id") else None,
                    title=item.get("title"),
                    company=item.get("company_name") or company,
                    location=item.get("location"),
                    url=link,
                    description_raw=description,
                    posted_at=parse_recruitee_date(item.get("created_at")),
                    **extra,
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
