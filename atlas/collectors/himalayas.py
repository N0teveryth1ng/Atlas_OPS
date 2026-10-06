"""Himalayas remote-jobs API collector.

Public JSON API (https://himalayas.app/jobs/api), max 20 jobs per page. Returns a
single page per fetch; rate limiting answers 429 with Retry-After, which the
shared HttpFetcher honours.

Shape verified against the live API on 2026-10-06:

* job objects carry **no** ``location`` key at all — eligibility lives in
  ``locationRestrictions``, a flat list of alternative countries that is empty when
  the role is unrestricted. ``timezoneRestrictions`` (UTC offsets) is separate.
* ``seniority`` is a list (``["Entry-level", "Mid-level"]``), and the search endpoint
  accepts a ``seniority`` filter that it validates: anything outside the documented
  values answers ``400 {"ok": false, "errors": "Invalid seniority"}``.
* there is no ``id`` key; ``guid``/``applicationLink`` identify the job.
* ``query`` is accepted but **ignored** — ``?query=zzzzzunobtainium`` returns the
  same jobs, in the same order, as no query at all. The local needle pass below is
  therefore the only text matching this source has.
"""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_URL = "https://himalayas.app/jobs/api/search"
PAGE_SIZE = 20
#: Sent on every request. The API validates it, and it is what makes this source
#: entry-level only rather than the whole feed.
SENIORITY = "Entry-level"
#: ``locationRestrictions`` entries are alternatives ("Canada" *or* "Germany"), never
#: a hierarchy, so a plain " / " join must not imply city/sub-country containment.
LOCATION_SEPARATOR = " / "
#: Every payload field the local needle pass searches. The API ignores ``query``, so
#: this pass is this source's only text match; limiting it to the three mapped fields
#: would drop jobs whose match lives in the excerpt, the category slugs or the
#: location restrictions.
SEARCH_FIELDS = (
    "title",
    "companyName",
    "description",
    "excerpt",
    "categories",
    "parentCategories",
    "locationRestrictions",
)


def _search_text(item: dict) -> str:
    """Flatten every searchable payload field into one lower-cased string."""
    parts: list[str] = []
    for field in SEARCH_FIELDS:
        value = item.get(field)
        if isinstance(value, (list, tuple)):
            parts.extend(str(part) for part in value if part)
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()


def _location(item: dict) -> str | None:
    """Map ``locationRestrictions`` into ``Job.location``.

    An empty list is the API's own "no restriction" signal and it documents nothing
    else, so the location stays ``None`` rather than invented.
    """
    restrictions = item.get("locationRestrictions")
    if not isinstance(restrictions, (list, tuple)):
        return None
    parts = [str(part).strip() for part in restrictions if str(part).strip()]
    if not parts:
        return None
    return LOCATION_SEPARATOR.join(dict.fromkeys(parts))


class HimalayasCollector(Collector):
    name = "himalayas"
    query_based = True

    def __init__(self, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        params: dict = {"limit": PAGE_SIZE, "seniority": SENIORITY}
        if needle:
            params["query"] = needle
        payload = self.get_json(API_URL, params) or {}
        jobs: list[Job] = []
        for item in payload.get("jobs") or []:
            link = item.get("applicationLink") or item.get("guid") or ""
            if not link:
                continue
            if needle and needle not in _search_text(item):
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id") or item.get("guid") or "") or None,
                    title=item.get("title"),
                    company=item.get("companyName"),
                    location=_location(item),
                    url=link,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("pubDate")),
                )
            )
        return jobs
