"""Teamtailor public job-board feed collector (per target subdomain).

Each careers site serves a public JSON Feed at::

    https://{subdomain}.teamtailor.com/jobs.json

The document is a JSON Feed: ``items`` carries the feed-reader fields
(``title``/``url``/``date_published``) plus an embedded ``_jobposting`` object
which is a schema.org ``JobPosting``. ``_jobposting`` is preferred when present
so the feed's own truncation of fields does not matter, with the top-level keys
as fallback. The link lives at the item level (``url``); ``_jobposting`` has no
``careers_url`` and no ``remote``/``location``/``requirements`` keys.

The official ``api.teamtailor.com/v1/jobs`` endpoint requires an API key and is
deliberately not used. No third-party licence for the public ``jobs.json`` feed
was verified, so this source stays opt-in pending owner confirmation.
"""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, HttpFetcher, parse_datetime

FEED_URL = "https://{subdomain}.teamtailor.com/jobs.json"


def _source_id(posting: dict, item: dict) -> str | None:
    """The feed item id is a uuid; the posting's identifier carries the stable
    numeric job id used in URLs, and is preferred when present."""
    identifier = posting.get("identifier")
    if isinstance(identifier, dict) and identifier.get("value") not in (None, ""):
        return str(identifier["value"])
    value = posting.get("id") or item.get("id")
    return str(value) if value not in (None, "") else None


def _company(posting: dict, subdomain: str) -> str:
    org = posting.get("hiringOrganization")
    if isinstance(org, dict) and org.get("name"):
        return str(org["name"])
    return subdomain


def _location(posting: dict) -> str | None:
    """Compose the first jobLocation's PostalAddress fields into a string."""
    locations = posting.get("jobLocation")
    if not isinstance(locations, list):
        return None
    for place in locations:
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if not isinstance(address, dict):
            continue
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        composed = ", ".join(part for part in parts if part)
        if composed:
            return composed
    return None


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
            link = item.get("url") or posting.get("careers_url") or ""
            if not link:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=_source_id(posting, item),
                    title=posting.get("title") or item.get("title"),
                    company=_company(posting, subdomain),
                    location=_location(posting),
                    url=link,
                    description_raw=str(posting.get("description") or ""),
                    posted_at=parse_datetime(
                        posting.get("datePosted") or item.get("date_published")
                    ),
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
