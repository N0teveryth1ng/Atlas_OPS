"""Sourcing orchestrator (plan sections 5.2–5.4).

Runs the query planner and every collector, normalizes + dedupes + freshness-
filters the pooled jobs, and records per-source / per-query yield.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .collectors import Collector, build_collectors
from .config import Settings, get_settings
from .db import upsert_job
from .normalize import dedupe_jobs, normalize_job
from .query_planner import plan_queries
from .schemas import CandidateProfile, Job

logger = logging.getLogger(__name__)


@dataclass
class SourceYield:
    source: str
    fetched: int = 0
    kept: int = 0
    passed_filters: int = 0
    sent: int = 0


@dataclass
class SourcingResult:
    queries: list[str] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    source_yields: list[SourceYield] = field(default_factory=list)
    query_yield: dict[str, int] = field(default_factory=dict)
    dropped_stale: int = 0
    new_jobs: int = 0


def apply_pipeline_yields(
    result: SourcingResult,
    processed: list,
    sent_ids: set[int],
) -> SourcingResult:
    """Fill in ``passed_filters`` and ``sent`` from an existing pipeline run.

    Mutates and returns ``result``. This only reads what the pipeline already
    decided — it never re-runs decisions or filters. A job is attributed to the
    source of its surviving canonical copy, which is how ``kept`` is counted too.

    ``sent_ids`` is the set of job ids that made it into the digest. Anything not
    in it (``--no-email``, an aborted send, an invariant violation) counts as not
    sent.
    """
    yields = {item.source: item for item in result.source_yields}
    for entry in processed:
        item = yields.get(entry.job.source)
        if item is None:
            continue
        if entry.filter_result is not None and entry.filter_result.passed:
            item.passed_filters += 1
        if entry.job_id is not None and entry.job_id in sent_ids:
            item.sent += 1
    return result


def is_fresh(job: Job, max_age_days: int, now: datetime) -> bool:
    if job.posted_at is None:
        return True
    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return (now - posted).days <= max_age_days


def collect(
    collectors: list[Collector],
    queries: list[str],
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    run_id: int | None = None,
    now: datetime | None = None,
) -> SourcingResult:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    result = SourcingResult(queries=list(queries))
    yields: dict[str, SourceYield] = {}
    pooled: list[Job] = []

    for collector in collectors:
        item = SourceYield(source=collector.name)
        yields[collector.name] = item
        source_queries = queries if collector.query_based else [""]
        for query in source_queries:
            raw = collector.safe_fetch(query)
            item.fetched += len(raw)
            kept = 0
            for job in raw:
                normalized = normalize_job(job, now=now)
                if not is_fresh(normalized, settings.filters.max_job_age_days, now):
                    result.dropped_stale += 1
                    continue
                pooled.append(normalized)
                kept += 1
            label = query or f"{collector.name} (board)"
            result.query_yield[label] = result.query_yield.get(label, 0) + kept

    deduped = dedupe_jobs(pooled)
    for job in deduped:
        if job.source in yields:
            yields[job.source].kept += 1
    result.jobs = deduped
    result.source_yields = list(yields.values())

    if conn is not None and run_id is not None:
        _persist(conn, run_id, queries, deduped, now, result)

    return result


def _persist(
    conn: sqlite3.Connection,
    run_id: int,
    queries: list[str],
    jobs: list[Job],
    now: datetime,
    result: SourcingResult,
) -> None:
    for query in queries:
        conn.execute(
            "INSERT INTO queries (run_id, query, created_at) VALUES (?, ?, ?)",
            (run_id, query, now.isoformat()),
        )
    conn.commit()
    for job in jobs:
        _, created = upsert_job(conn, run_id, job)
        if created:
            result.new_jobs += 1


def run_sourcing(
    profile: CandidateProfile,
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    run_id: int | None = None,
    collectors: list[Collector] | None = None,
    now: datetime | None = None,
) -> SourcingResult:
    settings = settings or get_settings()
    collectors = collectors if collectors is not None else build_collectors(settings)
    queries = plan_queries(profile, settings)
    return collect(collectors, queries, settings=settings, conn=conn, run_id=run_id, now=now)
