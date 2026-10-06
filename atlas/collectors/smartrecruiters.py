"""SmartRecruiters public postings API collector (per target company).

Two-step: ``/v1/companies/{company}/postings`` lists published postings, then
``/v1/companies/{company}/postings/{id}`` supplies the full description sections
and the apply link. Both endpoints are public and need no API key.

Caveat recorded in the fixture README: ``api.smartrecruiters.com/robots.txt`` is
``User-agent: * / Disallow: /`` even though the API is documented for third-party
applications. That conflict is why this source is opt-in and needs owner sign-off.
"""

from __future__ import annotations

import logging

from ..schemas import Job
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_ROOT = "https://api.smartrecruiters.com/v1/companies"
PAGE_LIMIT = 100

logger = logging.getLogger(__name__)


def _section_text(value: object) -> str | None:
    """Extract the text of a ``jobAd.sections`` value, dict or legacy string."""
    if isinstance(value, dict):
        return value.get("text")
    if value in (None, ""):
        return None
    return str(value)


class SmartRecruitersCollector(Collector):
    name = "smartrecruiters"
    query_based = False

    def __init__(self, companies: list[str], *, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())
        self.companies = companies
        self._cache: dict[str, list[Job]] = {}

    def _listing(self, company: str) -> list[dict]:
        content: list[dict] = []
        seen_ids: set[str] = set()
        offset = 0
        while True:
            payload = (
                self.get_json(
                    f"{API_ROOT}/{company}/postings",
                    {"limit": PAGE_LIMIT, "offset": offset},
                )
                or {}
            )
            page = list(payload.get("content") or [])
            if not page:
                # An exhausted listing starts returning empty pages, so a stale
                # or missing totalFound can never make this loop forever.
                break
            fresh = [
                item for item in page if item.get("id") is None or str(item["id"]) not in seen_ids
            ]
            if not fresh:
                # offset ignored and the same page repeated: stop rather than
                # loop forever over the same postings.
                break
            content.extend(fresh)
            seen_ids.update(str(item["id"]) for item in fresh if item.get("id") is not None)
            offset += len(page)
            if len(content) >= int(payload.get("totalFound") or 0):
                break
        return content

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
            try:
                detail = self._detail(company, str(posting_id))
            except Exception as exc:  # noqa: BLE001 - one bad posting must not drop the source
                logger.warning(
                    "smartrecruiters: detail for %s/%s failed, using listing fields: %s",
                    company,
                    posting_id,
                    exc,
                )
                detail = {}
            # The detail payload nests the description under `jobAd.sections`,
            # where each section is {title, text}. Older/tenant-variant responses
            # have been seen with bare-string sections flattened at the top
            # level, so accept either shape.
            job_ad = detail.get("jobAd") or {}
            sections = job_ad.get("sections") or detail.get("sections") or {}
            url = detail.get("postingUrl") or detail.get("applyUrl") or item.get("postingUrl") or ""
            if not url:
                continue
            location = detail.get("location") or item.get("location") or {}
            description = "\n\n".join(
                part
                for part in (
                    _section_text(sections[key])
                    for key in (
                        "jobDescription",
                        "additionalInformation",
                        "qualifications",
                        "responsibilities",
                    )
                    if sections.get(key)
                )
                if part
            )
            company_obj = detail.get("company") or item.get("company") or {}
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(posting_id),
                    title=detail.get("name") or item.get("name"),
                    company=company_obj.get("name") or company,
                    location=location.get("fullLocation") or location.get("city"),
                    url=url,
                    description_raw=description,
                    posted_at=parse_datetime(
                        detail.get("releasedDate")
                        or detail.get("created")
                        or item.get("releasedDate")
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
