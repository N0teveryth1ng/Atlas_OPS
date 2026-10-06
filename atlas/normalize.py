"""Normalizer + deduper (plan section 5.4).

Strips HTML/boilerplate, derives a stable dedupe key and merges cross-board
duplicates (same key, or same description signature) into one job with many URLs.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlparse

from .schemas import Job, RemoteType

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BOILERPLATE_MARKERS = [
    "equal opportunity",
    "we are an equal",
    "all qualified applicants",
    "eeo statement",
    "benefits:",
    "perks:",
    "why join us",
]


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def strip_boilerplate(text: str) -> str:
    lowered = text.lower()
    cut = len(text)
    for marker in _BOILERPLATE_MARKERS:
        index = lowered.find(marker)
        if index != -1:
            cut = min(cut, index)
    return text[:cut].strip()


def _norm_key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return _WS_RE.sub(" ", text).strip()


def make_dedupe_key(company: str | None, title: str | None, location: str | None) -> str:
    raw = "|".join([_norm_key(company or ""), _norm_key(title or ""), _norm_key(location or "")])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def description_signature(text: str, length: int = 400) -> str | None:
    normalized = _norm_key(text)[:length]
    if not normalized:
        return None
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]


def normalize_job(job: Job, *, now: datetime | None = None) -> Job:
    job = job.model_copy(deep=True)
    job.description_raw = strip_boilerplate(strip_html(job.description_raw))

    if job.remote_type == RemoteType.unknown:
        haystack = f"{job.title or ''} {job.location or ''} {job.description_raw}".lower()
        if "hybrid" in haystack:
            job.remote_type = RemoteType.hybrid
        elif "remote" in haystack or "work from home" in haystack:
            job.remote_type = RemoteType.remote

    job.dedupe_key = make_dedupe_key(job.company, job.title, job.location)
    job.fetched_at = now or datetime.now(UTC)
    if not job.urls:
        job.urls = [job.url]
    return job


#: Hosts whose pages are the employer's own ATS entry. When the same job is
#: found both on an ATS and on an aggregator, the ATS URL is the canonical one to
#: show a human: it is first-party, stable, and where you actually apply.
#: Order is significant - first match wins.
ATS_HOSTS = (
    "boards.greenhouse.io",
    "boards-api.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "apply.workable.com",
    "jobs.smartrecruiters.com",
    "recruitee.com",
    "teamtailor.com",
    "hire.lever.co",
)


def is_ats_url(url: str | None) -> bool:
    """True when the URL points at a known ATS board rather than an aggregator."""
    if not url:
        return False
    netloc = (urlparse(url).hostname or "").lower()
    # Match only on the hostname itself or a subdomain, never a bare substring
    # that could sit in a path or query string of an aggregator URL.
    return any(netloc == host or netloc.endswith("." + host) for host in ATS_HOSTS)


#: ``(host, collector name)`` pairs used to attribute a URL to the source that
#: contributed it. Matched with the same exact-host-or-dot-suffix rule as
#: :data:`ATS_HOSTS` (``sysmex.recruitee.com`` is Recruitee, a query string
#: containing ``boards.greenhouse.io`` is not Greenhouse); first match wins.
SOURCE_HOSTS: tuple[tuple[str, str], ...] = (
    ("himalayas.app", "himalayas"),
    ("jobicy.com", "jobicy"),
    ("arbeitnow.com", "arbeitnow"),
    ("remoteok.com", "remoteok"),
    ("remotive.com", "remotive"),
    ("weworkremotely.com", "weworkremotely"),
    ("boards.greenhouse.io", "greenhouse"),
    ("boards-api.greenhouse.io", "greenhouse"),
    ("job-boards.greenhouse.io", "greenhouse"),
    ("jobs.lever.co", "lever"),
    ("hire.lever.co", "lever"),
    ("jobs.ashbyhq.com", "ashby"),
    ("apply.workable.com", "workable"),
    ("jobs.smartrecruiters.com", "smartrecruiters"),
    ("recruitee.com", "recruitee"),
    ("teamtailor.com", "teamtailor"),
)


def source_for_url(url: str | None) -> str | None:
    """Collector ``name`` for a URL, or ``None`` when the host is unknown.

    Mirrors :func:`is_ats_url`'s discipline: the hostname must equal a known
    host or end with ``"." + host``. Used to credit every provider that
    contributed a URL to a merged job.
    """
    if not url:
        return None
    netloc = (urlparse(url).hostname or "").lower()
    for host, source in SOURCE_HOSTS:
        if netloc == host or netloc.endswith("." + host):
            return source
    return None


def _merge_into(target: Job, other: Job) -> None:
    urls = {target.url, other.url, *target.urls, *other.urls}
    target.urls = sorted(urls)
    # Prefer an ATS URL as canonical. Equal on both sides (or neither) keeps
    # whatever the target already had, so behaviour is unchanged there.
    if not is_ats_url(target.url) and is_ats_url(other.url):
        target.url = other.url


def dedupe_jobs(jobs: list[Job]) -> list[Job]:
    """Merge duplicates by dedupe key, then by description signature."""
    by_key: dict[str, Job] = {}
    for job in jobs:
        key = job.dedupe_key or make_dedupe_key(job.company, job.title, job.location)
        if key in by_key:
            _merge_into(by_key[key], job)
        else:
            job.dedupe_key = key
            by_key[key] = job

    result: list[Job] = []
    sig_index: dict[str, int] = {}
    for job in by_key.values():
        signature = description_signature(job.description_raw, 300)
        if signature and signature in sig_index:
            _merge_into(result[sig_index[signature]], job)
            continue
        if signature:
            sig_index[signature] = len(result)
        result.append(job)
    return result
