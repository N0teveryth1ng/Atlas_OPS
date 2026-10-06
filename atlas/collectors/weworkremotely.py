"""We Work Remotely RSS collector.

The site publishes an RSS feed at https://weworkremotely.com/remote-jobs.rss and
states at https://weworkremotely.com/remote-job-rss-feed that it may be used
provided the site is credited. That page is why this source is opt-in: the API
terms and guidelines separately forbid scraping or storing the data, so the owner
must confirm the RSS route is acceptable before enabling it.

Items use ``Company: Title``, and the feed adds non-standard ``region``,
``country`` and ``type`` elements alongside ``category``.
"""

from __future__ import annotations

from xml.etree import ElementTree

from ..schemas import Job, RemoteType
from .base import Collector, GetText, HttpFetcher, parse_http_date

FEED_URL = "https://weworkremotely.com/remote-jobs.rss"


def _text(element: ElementTree.Element, tag: str) -> str:
    found = element.find(tag)
    return (found.text or "").strip() if found is not None else ""


def _split_title(raw: str) -> tuple[str | None, str | None]:
    """The feed encodes company and role as ``Company: Title``."""
    if ": " not in raw:
        return None, raw or None
    company, _, title = raw.partition(": ")
    return company.strip() or None, title.strip() or None


class WeWorkRemotelyCollector(Collector):
    name = "weworkremotely"
    query_based = True

    def __init__(self, get_text: GetText | None = None):
        # The RSS feed needs raw text, so the default is the retrying fetcher
        # rather than the JSON path.
        super().__init__(get_text=get_text or HttpFetcher().fetch_text)

    def fetch(self, query: str) -> list[Job]:
        root = ElementTree.fromstring(self.get_text(FEED_URL, None))
        jobs: list[Job] = []
        for item in root.iterfind("./channel/item"):
            url = _text(item, "link")
            if not url:
                continue
            company, title = _split_title(_text(item, "title"))
            jobs.append(
                Job(
                    source=self.name,
                    source_id=_text(item, "guid") or None,
                    title=title,
                    company=company,
                    location=_text(item, "country") or _text(item, "region") or None,
                    remote_type=RemoteType.remote,
                    url=url,
                    description_raw=_text(item, "description"),
                    posted_at=parse_http_date(_text(item, "pubDate")),
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
