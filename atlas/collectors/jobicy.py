"""Jobicy remote-jobs API collector.

Public API at https://jobicy.com/api/v2/remote-jobs. No key required, and the
site publishes an AI catalog / MCP server, so automated reads are expected.

The API's ``friendlyNotice`` requires crediting Jobicy as the source and linking
to the original application URL, so the listing ``url`` is kept on the Job
alongside whatever direct apply link the item carries.

Shape verified against the live API on 2026-10-06:

* there is no free-text ``q`` parameter — sending one answers ``400
  {"success": false, "error": "Unexpected parameter 'q'"}``. Keyword search is
  ``tag``, and the API rejects a ``tag`` shorter than 3 or longer than 50
  characters with ``400 The length of the 'tag' value should be between 3 and 50
  characters``.
* job objects are named ``jobTitle``/``companyName``/``jobGeo``/``jobDescription``;
  there is no ``title``, ``location``, ``description`` or ``remote`` key at all.
* ``pubDate`` is an ISO-8601 string (``2026-10-05T18:58:00+00:00``), not epoch
  seconds.
* every job on this endpoint is remote by definition, so ``remote_type`` records
  :attr:`RemoteType.remote` rather than waiting for a field that never arrives.
"""

from __future__ import annotations

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, HttpFetcher, parse_datetime

API_URL = "https://jobicy.com/api/v2/remote-jobs"
PAGE_SIZE = 50
#: ``tag`` is rejected with 400 outside this range, so queries that fall outside it
#: are only filtered locally.
TAG_MIN_LENGTH = 3
TAG_MAX_LENGTH = 50
#: Every textual job-content field the local needle pass searches. ``tag`` narrows
#: server-side when it can be sent, but a 1-2 character query cannot use it at all,
#: so the local pass is the whole match there; excluding it would return an
#: arbitrary unfiltered page.
SEARCH_FIELDS = (
    "jobTitle",
    "companyName",
    "jobDescription",
    "jobExcerpt",
    "jobIndustry",
    "jobLevel",
    "jobType",
    "jobSlug",
    "jobGeo",
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


class JobicyCollector(Collector):
    name = "jobicy"
    query_based = True

    def __init__(self, get_json: GetJson | None = None):
        super().__init__(get_json or HttpFetcher())

    def fetch(self, query: str) -> list[Job]:
        needle = (query or "").lower().strip()
        params: dict = {"count": PAGE_SIZE}
        if needle and TAG_MIN_LENGTH <= len(needle) <= TAG_MAX_LENGTH:
            params["tag"] = needle
        payload = self.get_json(API_URL, params) or {}
        jobs: list[Job] = []
        for item in payload.get("jobs") or []:
            url = item.get("url") or ""
            if not url:
                continue
            if needle and needle not in _search_text(item):
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item["id"]) if item.get("id") else None,
                    title=item.get("jobTitle"),
                    company=item.get("companyName"),
                    location=item.get("jobGeo") or None,
                    remote_type=RemoteType.remote,
                    url=url,
                    description_raw=item.get("jobDescription") or "",
                    posted_at=parse_datetime(item.get("pubDate")),
                )
            )
        return jobs
