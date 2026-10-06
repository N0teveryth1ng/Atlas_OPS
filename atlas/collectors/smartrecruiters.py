"""SmartRecruiters public postings API collector (per target company).

Two-step: ``/v1/companies/{company}/postings`` lists published postings, then
``/v1/companies/{company}/postings/{id}`` supplies the full description sections
and the apply link. Both endpoints are public and need no API key.

Caveat recorded in the fixture README: ``api.smartrecruiters.com/robots.txt`` is
``User-agent: * / Disallow: /`` even though the API is documented for third-party
applications. That conflict is why this source is opt-in and needs owner sign-off.
"""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_ROOT = "https://api.smartrecruiters.com/v1/companies"
PAGE_LIMIT = 100


class SmartRecruitersCollector(Collector):
    name = "smartrecruiters"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _listing(self, company: str) -> list[dict]:
        payload = self.get_json(f"{API_ROOT}/{company}/postings", {"limit": PAGE_LIMIT}) or {}
        return list(payload.get("content") or [])

    def _detail(self, company: str, posting_id: str) -> dict:
        payload = self.get_json(f"{API_ROOT}/{company}/postings/{posting_id}", None) or {}
        return payload if isinstance(payload, dict) else {}

    def _board(self, company: str) -> list[Job]:
        if company in self._cache:
            return self._cache[company]
        jobs: list[Job] = []
        for item in self._listing(company):
            posting_id = item.get("id")
            if posting_id is None:
                continue
            detail = self._detail(company, str(posting_id))
            sections = detail.get("sections") or {}
            url = detail.get("postingUrl") or detail.get("applyUrl") or item.get("postingUrl") or ""
            if not url:
                continue
            location = detail.get("location") or item.get("location") or {}
            description = "\n\n".join(
                str(sections[key])
                for key in (
                    "jobDescription",
                    "additionalInformation",
                    "qualifications",
                    "responsibilities",
                )
                if sections.get(key)
            )
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(posting_id),
                    title=detail.get("title") or item.get("title"),
                    company=detail.get("companyName") or company,
                    location=location.get("displayName") or location.get("city"),
                    url=url,
                    description_raw=description,
                    posted_at=parse_datetime(
                        detail.get("modified") or detail.get("created") or item.get("created")
                    ),
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
