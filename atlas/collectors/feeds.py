"""Free remote-job feeds: RemoteOK and Remotive (plan section 5.3)."""

from __future__ import annotations

from ..schemas import Job, RemoteType
from .base import Collector, GetJson, parse_datetime


class RemoteOKCollector(Collector):
    name = "remoteok"
    query_based = False

    def __init__(self, *, get_json: GetJson | None = None):
        super().__init__(get_json)

    def fetch(self, query: str) -> list[Job]:
        payload = self.get_json("https://remoteok.com/api", None)
        needle = (query or "").lower().strip()
        jobs: list[Job] = []
        for item in payload or []:
            link = item.get("url") or item.get("apply_url") or ""
            title = item.get("position") or item.get("title")
            if not link or not title:
                continue
            if needle and needle not in title.lower():
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") else None,
                    title=title,
                    company=item.get("company"),
                    location=item.get("location") or "Remote",
                    remote_type=RemoteType.remote,
                    url=link,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("date") or item.get("epoch")),
                )
            )
        return jobs


class RemotiveCollector(Collector):
    name = "remotive"

    def __init__(self, *, limit: int = 50, get_json: GetJson | None = None):
        super().__init__(get_json)
        self.limit = limit

    def fetch(self, query: str) -> list[Job]:
        payload = self.get_json(
            "https://remotive.com/api/remote-jobs",
            {"search": query, "limit": self.limit},
        )
        jobs: list[Job] = []
        for item in (payload or {}).get("jobs", []):
            link = item.get("url") or ""
            if not link:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") else None,
                    title=item.get("title"),
                    company=item.get("company_name"),
                    location=item.get("candidate_required_location") or "Remote",
                    remote_type=RemoteType.remote,
                    url=link,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("publication_date")),
                )
            )
        return jobs
