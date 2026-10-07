"""Explicit pipeline: parse -> filter -> skill match -> evaluate -> verify -> rank.

Each stage stores its inputs/outputs via ``log_stage`` so any job can be traced
and a run is replayable (plan section 6). This is a plain state machine, not a
free-roaming agent loop.

Each stage is additionally wrapped in an optional Braintrust span, opened with
:func:`atlas.observability.traced_stage`. That wrapping is observational only: it
records allowlisted fields and the stage's latency, and a span that cannot be
created or ended leaves the stage's own behaviour untouched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import Settings, get_settings
from .db import log_stage, set_job_status
from .decision import evaluate_decision
from .evaluator import evaluate_job
from .evidence import EvidenceValidationError
from .filters import apply_hard_filters
from .jd_parser import parse_jd
from .job_status import JobStatus, transition
from .llm import LLMClient
from .observability import traced_stage
from .schemas import (
    CandidateProfile,
    Decision,
    DecisionOutcome,
    FilterResult,
    Job,
    ParsedJD,
    Recommendation,
    RemoteType,
    Verdict,
    VerifierVerdict,
)
from .skills import SkillMatch, match_skills
from .verifier import verify_job

logger = logging.getLogger(__name__)


def _job_fields(job_id: int | None, job: Job) -> dict[str, Any]:
    """The only job attributes a trace may carry: id, title, company and url."""
    return {
        "job_id": job_id,
        "job_title": job.title,
        "job_company": job.company,
        "job_url": job.url,
    }


@dataclass
class ProcessedJob:
    job_id: int | None
    job: Job
    parsed: ParsedJD | None = None
    filter_result: FilterResult | None = None
    skill_match: SkillMatch | None = None
    verdict: Verdict | None = None
    verifier: VerifierVerdict | None = None
    decision: Decision | None = None
    final_recommendation: Recommendation = Recommendation.skip
    score: float = 0.0
    needs_review: bool = False
    evidence_failed: bool = False
    status: JobStatus = JobStatus.new

    def advance(self, new_status: JobStatus, conn: sqlite3.Connection | None = None) -> None:
        """Move to ``new_status``, enforcing the state machine; optionally persist."""
        self.status = transition(self.status, new_status)
        if conn is not None and self.job_id is not None:
            set_job_status(conn, self.job_id, new_status)


def _recommendation_for(decision: Decision) -> Recommendation:
    """Map a DecisionOutcome onto the Recommendation enum the digest expects."""
    if decision.outcome == DecisionOutcome.apply:
        return Recommendation.strong_apply if decision.match_score >= 90.0 else Recommendation.apply
    if decision.outcome == DecisionOutcome.review:
        return Recommendation.maybe
    return Recommendation.skip


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_jobs(
    conn: sqlite3.Connection,
    *,
    run_id: int | None = None,
    limit: int | None = None,
    statuses: tuple[JobStatus, ...] | None = (JobStatus.new,),
) -> list[tuple[int, Job]]:
    """Stored jobs to process, newest first.

    ``statuses`` defaults to ``new`` only. A job that already finished (``rejected``,
    ``needs_review``, ``sent``) or was left mid-pipeline by an interrupted run cannot
    legally restart at ``parsed``, so selecting it would spend the run on
    IllegalTransition warnings while the digest reported "no matches". Callers that
    deliberately want every row pass ``statuses=None``.
    """
    sql = (
        "SELECT id, source, source_id, dedupe_key, title, company, location, remote_type, "
        "url, urls_json, description_raw, posted_at, fetched_at FROM jobs"
    )
    clauses: list[str] = []
    params: list = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(status.value for status in statuses)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    out: list[tuple[int, Job]] = []
    for row in conn.execute(sql, tuple(params)).fetchall():
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

    job_fields = _job_fields(job_id, job)

    with traced_stage(
        "pipeline.parse",
        stage="parse",
        model=settings.models.extractor,
        prompt_version="jd_parser",
        **job_fields,
    ):
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

    with traced_stage("pipeline.filters", stage="filters", **job_fields) as filters_span:
        filter_result = apply_hard_filters(job, parsed, profile, settings)
        filters_span.set_metadata(
            passed=filter_result.passed,
            rejection_reasons=[rejection.rule_id for rejection in filter_result.rejections],
        )
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
    with traced_stage("pipeline.skill_match", stage="skill_match", **job_fields) as skill_span:
        skill_match = match_skills(
            parsed.must_have_skills, parsed.nice_to_have_skills, profile.skills
        )
        skill_span.set_metadata(
            must_have_coverage=skill_match.must_have_coverage,
            nice_to_have_coverage=skill_match.nice_to_have_coverage,
        )
    result.skill_match = skill_match

    try:
        with traced_stage(
            "pipeline.evaluator",
            stage="evaluator",
            model=settings.models.evaluator,
            prompt_version="evaluator",
            **job_fields,
        ) as evaluator_span:
            verdict = evaluate_job(
                client,
                profile=profile,
                job=job,
                parsed_jd=parsed,
                skill_match=skill_match,
                settings=settings,
                cache=cache,
            )
            evaluator_span.set_metadata(
                score=verdict.fit_score,
                recommendation=verdict.recommendation.value,
            )
        result.verdict = verdict
        result.advance(JobStatus.evaluated, conn)

        with traced_stage(
            "pipeline.verifier",
            stage="verifier",
            model=settings.models.verifier,
            prompt_version="verifier",
            **job_fields,
        ) as verifier_span:
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
            verifier_span.set_metadata(
                passed=not verifier.veto,
                recommendation=(
                    verifier.downgrade_to.value if verifier.downgrade_to is not None else None
                ),
            )
        result.verifier = verifier
        result.advance(JobStatus.verified, conn)
    except EvidenceValidationError as exc:
        logger.warning("job %s: unverifiable evidence: %s", job_id, exc)
        result.evidence_failed = True
        result.needs_review = True
        result.advance(JobStatus.needs_review, conn)
        result.final_recommendation = Recommendation.maybe
        return result

    try:
        with traced_stage(
            "pipeline.decision",
            stage="decision",
            model=settings.models.evaluator,
            prompt_version="decision",
            **job_fields,
        ) as decision_span:
            decision = evaluate_decision(
                client,
                profile=profile,
                job=job,
                parsed=parsed,
                verdict=verdict,
                verifier=verifier,
                skill_match=skill_match,
                settings=settings,
                cache=cache,
            )
            # Nested dict keys are filtered out by the payload allowlist, so the
            # model/prompt maps are flattened to the names they carry.
            decision_span.set_metadata(
                decision=decision.outcome.value,
                match_score=decision.match_score,
                confidence=decision.confidence,
                veto_reasons=[veto.rule_id for veto in decision.vetoes],
                model_names=sorted(set(decision.model_names.values())),
                prompt_versions=sorted(set(decision.prompt_versions.values())),
            )
    except EvidenceValidationError as exc:
        logger.warning("job %s: decision evidence failed: %s", job_id, exc)
        result.evidence_failed = True
        result.needs_review = True
        result.advance(JobStatus.needs_review, conn)
        result.final_recommendation = Recommendation.maybe
        return result

    result.decision = decision
    result.score = decision.match_score
    result.final_recommendation = _recommendation_for(decision)
    if decision.outcome == DecisionOutcome.review:
        result.needs_review = True
        result.advance(JobStatus.needs_review, conn)
    else:
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
        log_stage(
            conn,
            "decisions",
            job_id=job_id,
            model=settings.models.evaluator,
            prompt_version="decision",
            data=decision.model_dump(),
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
    stored = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    if stored > len(jobs):
        logger.info(
            "Pipeline: skipping %d stored job(s) that are already processed or mid-run",
            stored - len(jobs),
        )
    with traced_stage("pipeline.run", stage="run", run_id=run_id, jobs=len(jobs)) as run_span:
        for job_id, job in jobs:
            try:
                processed.append(
                    process_job(client, job_id, job, profile, settings, cache=cache, conn=conn)
                )
            except Exception as exc:  # noqa: BLE001 - one bad job must not kill the run
                logger.warning("job %s failed: %s", job_id, exc)
        run_span.set_metadata(
            processed=len(processed),
            filtered_out=sum(
                1 for r in processed if r.filter_result is not None and not r.filter_result.passed
            ),
            evaluated=sum(1 for r in processed if r.verdict is not None),
        )
    return processed
