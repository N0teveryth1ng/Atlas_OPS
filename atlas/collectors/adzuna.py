"""Adzuna collector (official API). Requires ADZUNA_APP_ID / ADZUNA_APP_KEY."""

from __future__ import annotations

from ..schemas import Job
from .base import Collector, GetJson, parse_datetime


class AdzunaCollector(Collector):
    name = "adzuna"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        *,
        country: str = "in",
        results_per_page: int = 50,
        get_json: GetJson | None = None,
    ):
        super().__init__(get_json)
        self.app_id = app_id
        self.app_key = app_key
        self.country = country
        self.results_per_page = results_per_page

    @property
    def available(self) -> bool:
        return bool(self.app_id and self.app_key)

    def fetch(self, query: str) -> list[Job]:
        if not self.available:
            return []
        url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1"
        payload = self.get_json(
            url,
            {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "what": query,
                "results_per_page": self.results_per_page,
                "content-type": "application/json",
            },
        )
        jobs: list[Job] = []
        for item in (payload or {}).get("results", []):
            url_ = item.get("redirect_url") or ""
            if not url_:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")) if item.get("id") is not None else None,
                    title=item.get("title"),
                    company=(item.get("company") or {}).get("display_name"),
                    location=(item.get("location") or {}).get("display_name"),
                    url=url_,
                    description_raw=item.get("description") or "",
                    posted_at=parse_datetime(item.get("created")),
                )
            )
        return jobs
