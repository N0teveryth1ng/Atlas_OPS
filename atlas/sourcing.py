"""Sourcing orchestrator (plan sections 5.2–5.4).

Runs the query planner and every collector, normalizes + dedupes + freshness-
filters the pooled jobs, and records per-source / per-query yield.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .collectors import Collector, build_collectors
from .config import Settings, get_settings
from .db import upsert_job
from .normalize import dedupe_jobs, normalize_job
from .query_planner import plan_queries
from .schemas import CandidateProfile, Job

logger = logging.getLogger(__name__)

#: Hard wall-clock bound for one source's fetch, across all of its queries.
SOURCE_TIMEOUT_SECONDS = 60.0
#: Hard wall-clock bound for the whole collection phase, across every source.
COLLECTION_TIMEOUT_SECONDS = 300.0

SourceEvent = Callable[[dict[str, Any]], None]


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
    persisted_job_sources: dict[int, str] = field(default_factory=dict)
    #: One message per source that timed out or raised; the run continues.
    source_errors: dict[str, str] = field(default_factory=dict)


def apply_pipeline_yields(
    result: SourcingResult,
    processed: list,
    sent_ids: set[int],
) -> SourcingResult:
    """Fill in ``passed_filters`` and ``sent`` from an existing pipeline run.

    Mutates and returns ``result``. This only reads what the pipeline already
    decided — it never re-runs decisions or filters. A job is attributed to the
    source of its surviving canonical copy, which is how ``kept`` is counted too.

    Only jobs persisted by *this* run (ids in ``persisted_job_sources``) can
    affect the counts: older stored jobs from earlier runs share the DB and are
    re-processed by the pipeline, but they must never inflate a current source.

    ``sent_ids`` is the set of job ids that made it into the digest; anything
    not in it (an aborted send, an invariant violation) counts as not sent.
    Email delivery was cancelled, so ``_execute_run`` passes an empty set and
    ``sent`` stays 0 for every source.
    """
    yields = {item.source: item for item in result.source_yields}
    for entry in processed:
        if entry.job_id is None:
            continue
        source = result.persisted_job_sources.get(entry.job_id)
        if source is None:
            continue
        item = yields.get(source)
        if item is None:
            continue
        if entry.filter_result is not None and entry.filter_result.passed:
            item.passed_filters += 1
        if entry.job_id in sent_ids:
            item.sent += 1
    return result


def is_fresh(job: Job, max_age_days: int, now: datetime) -> bool:
    if job.posted_at is None:
        return True
    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    return (now - posted).days <= max_age_days


def _fetch_with_timeout(
    collector: Collector, query: str, timeout: float
) -> tuple[list[Job] | None, str | None]:
    """Run ``collector.fetch`` in a daemon thread with a hard wall-clock bound.

    A blocking ``fetch`` cannot be interrupted in Python, so the thread is left
    to finish on its own (daemon, so it never holds the process up) while the
    caller records a timeout and moves on to the next source. Returns
    ``(jobs, None)`` on success and ``(None, message)`` on timeout or error.
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["jobs"] = collector.fetch(query)
        except BaseException as exc:  # noqa: BLE001 - one dead source must not kill the run
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, name=f"fetch-{collector.name}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return None, f"timed out after {timeout:.0f}s"
    if "error" in box:
        return None, str(box["error"])
    jobs = box.get("jobs")
    return (list(jobs) if isinstance(jobs, list) else []), None


def collect(
    collectors: list[Collector],
    queries: list[str],
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    run_id: int | None = None,
    now: datetime | None = None,
    source_timeout: float | None = None,
    run_timeout: float | None = None,
    on_source: SourceEvent | None = None,
) -> SourcingResult:
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    source_timeout = SOURCE_TIMEOUT_SECONDS if source_timeout is None else source_timeout
    run_timeout = COLLECTION_TIMEOUT_SECONDS if run_timeout is None else run_timeout
    deadline = time.monotonic() + run_timeout
    result = SourcingResult(queries=list(queries))
    yields: dict[str, SourceYield] = {}
    pooled: list[Job] = []

    def emit_source(source: str, phase: str, **fields: Any) -> None:
        if on_source is not None:
            on_source({"source": source, "phase": phase, **fields})

    for collector in collectors:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.error("collection run timed out before source %s", collector.name)
            result.source_errors[collector.name] = "collection run timed out"
            emit_source(collector.name, "error", error="collection run timed out")
            continue
        item = SourceYield(source=collector.name)
        yields[collector.name] = item
        source_queries = queries if collector.query_based else [""]
        timeout = min(source_timeout, remaining)
        source_started = time.monotonic()
        logger.info("source %s: start", collector.name)
        emit_source(collector.name, "start")
        error: str | None = None
        for query in source_queries:
            raw, query_error = _fetch_with_timeout(collector, query, timeout)
            if query_error is not None:
                error = query_error
                logger.error("source %s: query %r failed: %s", collector.name, query, query_error)
                break
            assert raw is not None
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
        elapsed = round(time.monotonic() - source_started, 2)
        if error is not None:
            result.source_errors[collector.name] = error
            emit_source(collector.name, "error", error=error, elapsed=elapsed)
            logger.error("source %s: end after %.2fs (error)", collector.name, elapsed)
        else:
            emit_source(
                collector.name,
                "end",
                fetched=item.fetched,
                kept=item.kept,
                elapsed=elapsed,
            )
            logger.info(
                "source %s: end after %.2fs (fetched=%d)", collector.name, elapsed, item.fetched
            )

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
        job_id, created = upsert_job(conn, run_id, job)
        result.persisted_job_sources[job_id] = job.source
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
    source_timeout: float | None = None,
    run_timeout: float | None = None,
    on_source: SourceEvent | None = None,
) -> SourcingResult:
    settings = settings or get_settings()
    collectors = collectors if collectors is not None else build_collectors(settings)
    queries = plan_queries(profile, settings)
    return collect(
        collectors,
        queries,
        settings=settings,
        conn=conn,
        run_id=run_id,
        now=now,
        source_timeout=source_timeout,
        run_timeout=run_timeout,
        on_source=on_source,
    )
