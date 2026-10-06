"""Collector interface + shared HTTP helpers (plan section 5.3).

Two HTTP paths live here:

* ``default_get_json`` — the original helper, unchanged, still wrapped in
  tenacity's 3-attempt exponential backoff. Every pre-existing collector uses it.
* :class:`HttpFetcher` — the path new collectors use. It is an explicit, callable
  ``(url, params)`` object carrying its own :class:`HttpPolicy` (timeout,
  attempts, backoff bounds, ``Retry-After`` cap) and it also serves raw text so
  RSS/XML feeds can be read without a new dependency.

Both go through :func:`_http_get`, so the request shape and User-Agent stay in
one place.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..schemas import Job

logger = logging.getLogger(__name__)

GetJson = Callable[[str, dict | None], Any]
GetText = Callable[[str, dict | None], str]
USER_AGENT = "Atlas_OPS/1.0 (personal job matcher)"

DEFAULT_TIMEOUT = 20.0
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_MIN = 1.0
DEFAULT_BACKOFF_MAX = 8.0
#: Never sleep longer than this for a ``Retry-After``, however long the header says.
MAX_RETRY_AFTER_SECONDS = 120.0
#: Statuses that carry a ``Retry-After`` and should back off rather than fail hard.
RETRYABLE_STATUS = frozenset({429, 503})


class RetryableStatusError(Exception):
    """A retryable HTTP status (429/503) carrying the server's ``Retry-After``."""

    def __init__(self, status_code: int, retry_after: float | None = None) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        suffix = f" (Retry-After: {retry_after}s)" if retry_after is not None else ""
        super().__init__(f"HTTP {status_code}{suffix}")


def _http_get(
    url: str, params: dict | None, *, timeout: float, headers: dict[str, str] | None = None
) -> httpx.Response:
    """The single request primitive: one GET, our User-Agent, explicit timeout.

    ``headers`` are merged under the User-Agent: the caller may add headers but
    can never override the identifying User-Agent. ``None`` (the default) is
    exactly the historical single-header request. Headers are never logged.
    """
    merged = {**(headers or {}), "User-Agent": USER_AGENT}
    return httpx.get(url, params=params or {}, timeout=timeout, headers=merged)


@retry(
    reraise=True,
    stop=stop_after_attempt(DEFAULT_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(httpx.HTTPError),
)
def _http_get_json(url: str, params: dict | None = None, *, timeout: float = 20.0) -> Any:
    response = _http_get(url, params, timeout=timeout)
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
        return datetime.fromtimestamp(timestamp, tz=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_http_date(value: str | None) -> datetime | None:
    """Parse an RFC 7231 / RFC 822 date (``Retry-After``, RSS ``pubDate``)."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait for a ``Retry-After`` header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(float(text), 0.0)
    except ValueError:
        pass
    stamp = parse_http_date(text)
    if stamp is None:
        return None
    reference = now or datetime.now(UTC)
    return max((stamp - reference).total_seconds(), 0.0)


def _should_retry(error: BaseException) -> bool:
    """Retry transport failures, 5xx, and rate limits — but not permanent 4xx.

    Only the new :class:`HttpFetcher` path applies this; ``_http_get_json`` keeps
    its historical retry-everything behaviour.
    """
    if isinstance(error, RetryableStatusError):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500
    return isinstance(error, httpx.HTTPError)


@dataclass(frozen=True)
class HttpPolicy:
    """How one source is fetched: timeout, attempts, backoff, ``Retry-After`` cap.

    ``sleep_seconds`` overrides the real sleep between attempts. Tests set it to
    ``0.0`` so a retry loop costs no wall-clock time; production leaves it ``None``.
    ``headers`` are extra request headers merged under the User-Agent (which the
    caller can never override); ``None`` means today's single-header request.
    """

    timeout: float = DEFAULT_TIMEOUT
    max_attempts: int = DEFAULT_ATTEMPTS
    backoff_multiplier: float = 1.0
    backoff_min: float = DEFAULT_BACKOFF_MIN
    backoff_max: float = DEFAULT_BACKOFF_MAX
    max_retry_after: float = MAX_RETRY_AFTER_SECONDS
    sleep_seconds: float | None = None
    headers: dict[str, str] | None = None


def _wait_seconds(policy: HttpPolicy) -> Callable[[RetryCallState], float]:
    """Build a backoff that honours ``Retry-After`` when the failure carried one."""
    fallback = wait_exponential(
        multiplier=policy.backoff_multiplier,
        min=policy.backoff_min,
        max=policy.backoff_max,
    )

    def wait(retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        if outcome is not None and outcome.failed:
            error = outcome.exception()
            if isinstance(error, RetryableStatusError) and error.retry_after is not None:
                return min(error.retry_after, policy.max_retry_after)
        return float(fallback(retry_state))

    return wait


class HttpFetcher:
    """Callable ``(url, params)`` returning JSON, with retries and ``Retry-After``.

    Satisfies the :data:`GetJson` injection point, so any collector that accepts
    ``get_json`` can be given one of these. :meth:`fetch_text` is the sibling
    injection point used by XML/Atom feeds.
    """

    def __init__(self, policy: HttpPolicy | None = None) -> None:
        self.policy = policy or HttpPolicy()
        self._fetch_json = self._retrying(self.get_json)
        self._fetch_text = self._retrying(self.get_text)

    def _retrying(self, call: Callable[[str, dict | None], Any]) -> Callable[..., Any]:
        policy = self.policy

        @retry(
            reraise=True,
            stop=stop_after_attempt(policy.max_attempts),
            wait=_wait_seconds(policy),
            retry=retry_if_exception(_should_retry),
            sleep=self._sleep,
        )
        def attempt(url: str, params: dict | None = None) -> Any:
            return call(url, params)

        return attempt

    def _sleep(self, seconds: float) -> None:
        override = self.policy.sleep_seconds
        delay = seconds if override is None else override
        logger.debug("retrying after %.1fs (requested %.1fs)", delay, seconds)
        time.sleep(delay)

    def request(self, url: str, params: dict | None = None) -> httpx.Response:
        """One attempt. 429/503 become :class:`RetryableStatusError`; other errors raise."""
        if self.policy.headers:
            response = _http_get(
                url, params, timeout=self.policy.timeout, headers=self.policy.headers
            )
        else:
            # No configured headers: call the primitive exactly as before, so
            # callers with no headers keep the identical request shape.
            response = _http_get(url, params, timeout=self.policy.timeout)
        if response.status_code in RETRYABLE_STATUS:
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            logger.warning(
                "GET %s -> HTTP %s (Retry-After: %s)", url, response.status_code, retry_after
            )
            raise RetryableStatusError(response.status_code, retry_after)
        response.raise_for_status()
        return response

    def get_json(self, url: str, params: dict | None = None) -> Any:
        """A single request with no retry — collectors should call :meth:`fetch_json`."""
        return self.request(url, params).json()

    def get_text(self, url: str, params: dict | None = None) -> str:
        """A single request with no retry — collectors should call :meth:`fetch_text`."""
        return self.request(url, params).text

    def fetch_json(self, url: str, params: dict | None = None) -> Any:
        """GET and decode JSON, retrying retryable failures."""
        return self._fetch_json(url, params)

    def fetch_text(self, url: str, params: dict | None = None) -> str:
        """GET and decode text (XML/Atom feeds), retrying retryable failures."""
        return self._fetch_text(url, params)

    def __call__(self, url: str, params: dict | None = None) -> Any:
        return self.fetch_json(url, params)


class Collector(ABC):
    """A source of raw jobs. ``fetch`` must return canonical ``Job`` objects."""

    name: str = "base"
    query_based: bool = True

    def __init__(self, get_json: GetJson | None = None, *, get_text: GetText | None = None):
        self.get_json = get_json or default_get_json
        self.get_text = get_text or HttpFetcher().fetch_text

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
