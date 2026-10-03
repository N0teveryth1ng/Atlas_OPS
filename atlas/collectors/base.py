"""Collector interface + shared HTTP helpers (plan section 5.3)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..schemas import Job

logger = logging.getLogger(__name__)

GetJson = Callable[[str, dict | None], Any]
USER_AGENT = "Atlas_OPS/1.0 (personal job matcher)"


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(httpx.HTTPError),
)
def _http_get_json(url: str, params: dict | None = None, *, timeout: float = 20.0) -> Any:
    response = httpx.get(
        url, params=params or {}, timeout=timeout, headers={"User-Agent": USER_AGENT}
    )
    response.raise_for_status()
    return response.json()


def default_get_json(url: str, params: dict | None = None) -> Any:
    return _http_get_json(url, params)


def parse_datetime(value: Any) -> datetime | None:
    """Parse epoch seconds/millis or ISO-8601 into an aware UTC datetime."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class Collector(ABC):
    """A source of raw jobs. ``fetch`` must return canonical ``Job`` objects."""

    name: str = "base"
    query_based: bool = True

    def __init__(self, get_json: GetJson | None = None):
        self.get_json = get_json or default_get_json

    @abstractmethod
    def fetch(self, query: str) -> list[Job]:  # pragma: no cover - interface
        raise NotImplementedError

    def safe_fetch(self, query: str) -> list[Job]:
        """Fetch, converting any failure into an empty list (graceful degrade)."""
        try:
            return self.fetch(query)
        except Exception as exc:  # noqa: BLE001 - one dead source must not kill the run
            logger.warning("collector %s failed for query %r: %s", self.name, query, exc)
            return []
