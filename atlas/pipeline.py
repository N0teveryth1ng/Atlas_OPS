"""Explicit pipeline: parse -> filter -> skill match -> evaluate -> verify -> rank.

Each stage stores its inputs/outputs via ``log_stage`` so any job can be traced
and a run is replayable (plan section 6). This is a plain state machine, not a
free-roaming agent loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings, get_settings
from .db import log_stage, set_job_status
from .evaluator import evaluate_job
from .filters import apply_hard_filters
from .jd_parser import parse_jd
from .job_status import JobStatus, transition
from .llm import LLMClient
from .ranker import final_score
from .schemas import (
    CandidateProfile,
    FilterResult,
    Job,
    ParsedJD,
    Recommendation,
    RemoteType,
    Verdict,
    VerifierVerdict,
)
from .skills import SkillMatch, match_skills
from .verifier import resolve_recommendation, verify_job

logger = logging.getLogger(__name__)


@dataclass
class ProcessedJob:
    job_id: int | None
    job: Job
    parsed: ParsedJD | None = None
    filter_result: FilterResult | None = None
    skill_match: SkillMatch | None = None
    verdict: Verdict | None = None
    verifier: VerifierVerdict | None = None
    final_recommendation: Recommendation = Recommendation.skip
    score: float = 0.0
    needs_review: bool = False
    status: JobStatus = JobStatus.new

    def advance(self, new_status: JobStatus, conn: sqlite3.Connection | None = None) -> None:
        """Move to ``new_status``, enforcing the state machine; optionally persist."""
        self.status = transition(self.status, new_status)
        if conn is not None and self.job_id is not None:
            set_job_status(conn, self.job_id, new_status)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_jobs(
    conn: sqlite3.Connection, *, run_id: int | None = None, limit: int | None = None
) -> list[tuple[int, Job]]:
    sql = (
        "SELECT id, source, source_id, dedupe_key, title, company, location, remote_type, "
        "url, urls_json, description_raw, posted_at, fetched_at FROM jobs"
    )
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    sql += " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    out: list[tuple[int, Job]] = []
    for row in conn.execute(sql, params).fetchall():
        job = Job(
            source=row["source"],
            source_id=row["source_id"],
            dedupe_key=row["dedupe_key"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            remote_type=RemoteType(row["remote_type"] or "unknown"),
            url=row["url"],
            urls=json.loads(row["urls_json"] or "[]"),
            description_raw=row["description_raw"] or "",
            posted_at=_parse_dt(row["posted_at"]),
            fetched_at=_parse_dt(row["fetched_at"]),
        )
        out.append((row["id"], job))
    return out


def process_job(
    client: LLMClient,
    job_id: int | None,
    job: Job,
    profile: CandidateProfile,
    settings: Settings,
    *,
    cache: dict | None = None,
    conn: sqlite3.Connection | None = None,
) -> ProcessedJob:
    result = ProcessedJob(job_id=job_id, job=job)
    result.advance(JobStatus.parsed, conn)

    parsed = parse_jd(client, title=job.title or "", description=job.description_raw or "")
    result.parsed = parsed
    result.needs_review = parsed.confidence < settings.filters.min_parse_confidence
    if conn is not None:
        log_stage(
            conn,
            "parsed_jds",
            job_id=job_id,
            model=settings.models.extractor,
            prompt_version="jd_parser",
            data=parsed.model_dump(),
        )

    filter_result = apply_hard_filters(job, parsed, profile, settings)
    result.filter_result = filter_result
    if conn is not None:
        if filter_result.passed:
            log_stage(conn, "filter_results", job_id=job_id, passed=True, data={})
        else:
            for rejection in filter_result.rejections:
                log_stage(
                    conn,
                    "filter_results",
                    job_id=job_id,
                    passed=False,
                    rule_id=rejection.rule_id,
                    evidence=rejection.evidence,
                )
    if not filter_result.passed:
        result.advance(JobStatus.rejected, conn)
        result.final_recommendation = Recommendation.skip
        return result

    if result.needs_review:
        result.advance(JobStatus.needs_review, conn)
        result.final_recommendation = Recommendation.maybe
        return result

    result.advance(JobStatus.passed_filters, conn)
    skill_match = match_skills(parsed.must_have_skills, parsed.nice_to_have_skills, profile.skills)
    result.skill_match = skill_match

    verdict = evaluate_job(
        client,
        profile=profile,
        job=job,
        parsed_jd=parsed,
        skill_match=skill_match,
        settings=settings,
        cache=cache,
    )
    result.verdict = verdict
    result.advance(JobStatus.evaluated, conn)

    verifier = verify_job(
        client,
        profile=profile,
        job=job,
        parsed_jd=parsed,
        verdict=verdict,
        skill_match=skill_match,
        settings=settings,
        cache=cache,
    )
    result.verifier = verifier
    result.advance(JobStatus.verified, conn)

    final = resolve_recommendation(verdict, verifier)
    if (
        skill_match.must_have_coverage < settings.filters.must_have_coverage_floor
        and final in {Recommendation.apply, Recommendation.strong_apply}
    ):
        final = Recommendation.maybe
    result.final_recommendation = final
    result.score = final_score(verdict, skill_match, job, settings)
    result.advance(JobStatus.ranked, conn)

    if conn is not None:
        log_stage(
            conn,
            "evaluations",
            job_id=job_id,
            model=settings.models.evaluator,
            prompt_version="evaluator",
            data={
                "verdict": verdict.model_dump(),
                "skill_match": {
                    "must_have_coverage": skill_match.must_have_coverage,
                    "nice_to_have_coverage": skill_match.nice_to_have_coverage,
                    "missing_must_haves": skill_match.missing_must_haves,
                },
                "score": result.score,
            },
        )
        log_stage(
            conn,
            "verifications",
            job_id=job_id,
            model=settings.models.verifier,
            prompt_version="verifier",
            data=verifier.model_dump(),
        )
    return result


def run_pipeline(
    conn: sqlite3.Connection,
    run_id: int,
    profile: CandidateProfile,
    settings: Settings,
    client: LLMClient,
    *,
    limit: int | None = None,
    cache: dict | None = None,
) -> list[ProcessedJob]:
    settings = settings or get_settings()
    cache = cache if cache is not None else {}
    processed: list[ProcessedJob] = []
    jobs = load_jobs(conn, limit=limit)
    logger.info("Pipeline: processing %d job(s)", len(jobs))
    for job_id, job in jobs:
        try:
            processed.append(process_job(client, job_id, job, profile, settings, cache=cache, conn=conn))
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the run
            logger.warning("job %s failed: %s", job_id, exc)
    return processed
