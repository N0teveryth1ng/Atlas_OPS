"""Measure the pipeline against the owner's human labels (read-only).

``atlas eval-labels`` replays the *same* pipeline stages a collected job goes
through - cheap filters, parse, hard filters, skill match, evaluator, verifier,
decision - over every job in ``eval/labeling_pool.jsonl`` that the owner has
labelled in ``eval/labels.jsonl``. It answers one question the owner asked:
where does the system disagree with me?

Design constraints (owner-set):

* **Read-only on the real eval files.** The labels and pool files are read and
  never written; the real ``atlas.db`` is never opened for writing - the replay
  runs in a private temporary database. The owner's label text is evaluation
  data: it is never fed to any prompt, alias map, config or fixture.
* **Zero LLM calls for cheap rejections.** Jobs the deterministic pre-filter
  already rejects cost nothing; only survivors reach the LLM stages.
* **Clean stop at the daily limit.** The Groq daily-limit guard and the per-run
  call/token caps are respected; the run stops cleanly and reports how many jobs
  remain.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .config import Settings, get_settings
from .db import add_llm_tokens, connect, db_path, get_latest_profile, init_db, upsert_job
from .filters import apply_cheap_filters
from .llm import GroqDailyLimitReached, LLMClient, LLMError
from .pipeline import (
    LLMCallBudgetExceeded,
    LLMTokenBudgetExceeded,
    _BudgetedClient,
    process_job,
)
from .prompt_store import load_prompt
from .schemas import CandidateProfile, Decision, Job, RemoteType

DEFAULT_LABELS_PATH = Path("eval") / "labels.jsonl"
DEFAULT_POOL_PATH = Path("eval") / "labeling_pool.jsonl"
DEFAULT_REPORT_PATH = Path("eval") / "label_eval_report.md"
DEFAULT_CHEAP_REPORT_PATH = Path("eval") / "label_eval_cheap_report.md"


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def wilson_interval(successes: int, total: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval (95% by default) for a binomial proportion.

    Returns ``(low, high)`` in ``[0, 1]``. With ``total == 0`` the interval is
    the uninformative full range ``(0.0, 1.0)``.
    """
    if total <= 0:
        return (0.0, 1.0)
    phat = successes / total
    denom = 1.0 + z * z / total
    center = (phat + z * z / (2 * total)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def _pct(value: float) -> str:
    return f"{100.0 * value:.1f}%"


def _pct_ci(successes: int, total: int) -> str:
    """A percentage with its sample size and 95% Wilson interval next to it."""
    low, high = wilson_interval(successes, total)
    return f"{_pct(successes / total) if total else 'n/a'} ({successes}/{total}) [{_pct(low)}-{_pct(high)}]"


def _esc(text: str) -> str:
    """Escape pipe characters for safe Markdown table cells."""
    return text.replace("|", "\\|")


# --------------------------------------------------------------------------- #
# Duplicate collapsing
# --------------------------------------------------------------------------- #

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|bv|b\.v\.|nv|sa|s\.a\.|plc|corp|corporation|company|co)\b"
)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_company(name: str | None) -> str:
    """Lowercase, drop legal suffixes and punctuation, collapse whitespace."""
    text = _NON_ALNUM_RE.sub(" ", str(name or "").lower())
    text = _COMPANY_SUFFIX_RE.sub(" ", text)
    return " ".join(text.split())


def normalize_title(title: str | None) -> str:
    """Lowercase, drop punctuation and collapse whitespace for title equality."""
    text = _NON_ALNUM_RE.sub(" ", str(title or "").lower())
    return " ".join(text.split())


def duplicate_key(case: LabelEvalCase) -> tuple[str, str]:
    """The (normalized company, normalized title) a duplicate is collapsed on."""
    return (normalize_company(case.company), normalize_title(case.title))


# --------------------------------------------------------------------------- #
# Cases and predictions
# --------------------------------------------------------------------------- #


@dataclass
class LabelEvalCase:
    """One labelled pool job, ready to replay through the pipeline."""

    short_id: str
    pool_id: str
    url: str
    source: str
    source_id: str | None
    title: str
    company: str
    location: str
    remote_type: str
    posted_at: str | None
    fetched_at: str | None
    description: str
    owner_label: str
    owner_reason: str

    def to_job(self) -> Job:
        remote = (
            self.remote_type if self.remote_type in {r.value for r in RemoteType} else "unknown"
        )
        dedupe = f"{normalize_company(self.company)}|{normalize_title(self.title)}|{self.url}"
        return Job(
            source=self.source or "pool",
            source_id=self.source_id,
            title=self.title,
            company=self.company,
            location=self.location,
            remote_type=RemoteType(remote),
            url=self.url,
            urls=[self.url],
            description_raw=self.description,
            posted_at=_parse_dt(self.posted_at),
            fetched_at=_parse_dt(self.fetched_at),
            dedupe_key=dedupe,
        )


def _parse_dt(value: str | None):
    from datetime import UTC, datetime

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class SystemPrediction:
    """What the system concluded for one job, reduced to the audit essentials."""

    outcome: str
    score: float = 0.0
    confidence: float = 0.0
    fired_rule: str | None = None
    fired_rules: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    reached_llm: bool = False
    vetoes: list[dict[str, Any]] = field(default_factory=list)
    must_have_skills: list[str] = field(default_factory=list)
    nice_to_have_skills: list[str] = field(default_factory=list)
    skill_details: list[dict[str, Any]] = field(default_factory=list)
    must_have_coverage: float | None = None
    nice_to_have_coverage: float | None = None
    must_have_coverage_num: float | None = None
    must_have_coverage_den: int | None = None
    dimension_scores: dict[str, float] = field(default_factory=dict)
    cheap_status: str = ""
    regex_min_years: float | None = None
    regex_max_years: float | None = None
    regex_title_seniority: str = ""
    regex_remote_type: str = ""
    regex_years_quote: str | None = None
    # Evaluation status: ``"evaluated"`` when the pipeline produced a real
    # decision (confidence > 0, must-have data present), ``"incomplete"`` when
    # LLM calls were made but no proper decision resulted, ``"cheap"`` when the
    # job was rejected by the LLM-free pre-screen (zero LLM calls).
    status: str = "incomplete"
    reason: str | None = None
    # jd_parser prompt version used to parse this job, for version-aware resume.
    prompt_version: str | None = None
    # UTC timestamp when the job was (re-)evaluated.
    as_of: str | None = None
    # Per-LLM-call audit details: stage, model, tokens, retries, validation
    # failures, HTTP errors, ok/error.
    per_call: list[dict[str, Any]] = field(default_factory=list)


def _cheap_status(prediction: SystemPrediction) -> str:
    """``"rejected"``, ``"passed"`` or ``""`` for one prediction.

    ``""`` means the cheap gate was never the deciding stage (a full run that
    reached the LLM stages), so the case is not counted either way.
    """
    return prediction.cheap_status


@dataclass
class CaseResult:
    case: LabelEvalCase
    prediction: SystemPrediction

    @property
    def owner_apply(self) -> bool:
        return self.case.owner_label == "apply"

    @property
    def system_apply(self) -> bool:
        return self.prediction.outcome == "apply"

    @property
    def system_review(self) -> bool:
        return self.prediction.outcome == "review"

    @property
    def system_skip(self) -> bool:
        return self.prediction.outcome == "skip"

    @property
    def agreement(self) -> bool:
        """Apply/skip ground truth versus APPLY versus (REVIEW+SKIP) as not-apply."""
        return self.owner_apply == self.system_apply


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


@dataclass
class Metrics:
    """Confusion matrix and the headline figures for one result set."""

    cases: list[CaseResult] = field(default_factory=list)

    # -- confusion matrix: owner label (rows) x system outcome (cols) -------- #
    @property
    def owner_apply_system_apply(self) -> int:
        return sum(1 for c in self.cases if c.owner_apply and c.system_apply)

    @property
    def owner_apply_system_review(self) -> int:
        return sum(1 for c in self.cases if c.owner_apply and c.system_review)

    @property
    def owner_apply_system_skip(self) -> int:
        return sum(1 for c in self.cases if c.owner_apply and c.system_skip)

    @property
    def owner_skip_system_apply(self) -> int:
        return sum(1 for c in self.cases if not c.owner_apply and c.system_apply)

    @property
    def owner_skip_system_review(self) -> int:
        return sum(1 for c in self.cases if not c.owner_apply and c.system_review)

    @property
    def owner_skip_system_skip(self) -> int:
        return sum(1 for c in self.cases if not c.owner_apply and c.system_skip)

    @property
    def owner_apply_total(self) -> int:
        return sum(1 for c in self.cases if c.owner_apply)

    @property
    def owner_skip_total(self) -> int:
        return len(self.cases) - self.owner_apply_total

    @property
    def system_apply_total(self) -> int:
        return sum(1 for c in self.cases if c.system_apply)

    # -- headline figures --------------------------------------------------- #
    @property
    def precision_apply_numerator(self) -> int:
        """System APPLYs the owner also called apply."""
        return self.owner_apply_system_apply

    @property
    def precision_apply(self) -> float:
        total = self.system_apply_total
        return self.precision_apply_numerator / total if total else 0.0

    @property
    def recall_apply_numerator(self) -> int:
        """Owner-apply jobs the system caught as APPLY or REVIEW."""
        return self.owner_apply_system_apply + self.owner_apply_system_review

    @property
    def recall_apply(self) -> float:
        total = self.owner_apply_total
        return self.recall_apply_numerator / total if total else 0.0

    @property
    def seniority_false_pass(self) -> list[CaseResult]:
        """Owner said skip for a seniority/experience reason, system said APPLY/REVIEW."""
        from .labeling import SENIORITY_RE

        out: list[CaseResult] = []
        for case in self.cases:
            if case.owner_apply:
                continue
            if not SENIORITY_RE.search(case.case.owner_reason):
                continue
            if case.system_apply or case.system_review:
                out.append(case)
        return out

    def per_source(self) -> dict[str, Metrics]:
        grouped: dict[str, list[CaseResult]] = {}
        for case in self.cases:
            grouped.setdefault(case.case.source or "unknown", []).append(case)
        return {source: Metrics(cases=items) for source, items in sorted(grouped.items())}

    def per_rule(self) -> dict[str, int]:
        """How often each veto/filter rule fired, over the whole set."""
        counts: dict[str, int] = {}
        for case in self.cases:
            for rule in case.prediction.fired_rules:
                counts[rule] = counts.get(rule, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def disagreements(self) -> list[CaseResult]:
        return [case for case in self.cases if not case.agreement]


def collapse_duplicates(cases: Sequence[CaseResult]) -> list[CaseResult]:
    """Keep one case per (normalized company, normalized title).

    The first case in file order wins; later duplicates are dropped. This lets the
    report show metrics with and without the duplicate postings the pool contains.
    """
    seen: set[tuple[str, str]] = set()
    out: list[CaseResult] = []
    for case in cases:
        key = duplicate_key(case.case)
        if key in seen:
            continue
        seen.add(key)
        out.append(case)
    return out


# --------------------------------------------------------------------------- #
# Loading labels + pool
# --------------------------------------------------------------------------- #


def _load_jsonl(path: Path) -> list[dict]:
    import json

    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            rows.append(entry)
    return rows


def load_label_cases(
    labels_path: Path | str = DEFAULT_LABELS_PATH,
    pool_path: Path | str = DEFAULT_POOL_PATH,
) -> list[LabelEvalCase]:
    """Join ``labels.jsonl`` to ``labeling_pool.jsonl`` by ``pool_id``.

    Read-only. Only labels pointing at a pool entry with a URL are kept; the
    labels file is never validated or modified here (``label-check`` does that).
    """
    from .labeling_cli import short_id_of

    pool_by_id = {
        str(entry.get("pool_id", "")).strip(): entry
        for entry in _load_jsonl(Path(pool_path))
        if str(entry.get("pool_id", "")).strip()
    }
    cases: list[LabelEvalCase] = []
    for entry in _load_jsonl(Path(labels_path)):
        pool_id = str(entry.get("pool_id", "")).strip()
        pool_entry = pool_by_id.get(pool_id)
        if pool_entry is None:
            continue
        urls = pool_entry.get("urls")
        case = LabelEvalCase(
            short_id=short_id_of(pool_id),
            pool_id=pool_id,
            url=str(pool_entry.get("url", "")).strip(),
            source=str(pool_entry.get("source", "")).strip(),
            source_id=(
                str(pool_entry["source_id"]) if pool_entry.get("source_id") is not None else None
            ),
            title=str(pool_entry.get("title", "")).strip(),
            company=str(pool_entry.get("company", "")).strip(),
            location=str(pool_entry.get("location", "")).strip(),
            remote_type=str(pool_entry.get("remote_type", "")).strip(),
            posted_at=(str(pool_entry["posted_at"]) if pool_entry.get("posted_at") else None),
            fetched_at=(str(pool_entry["fetched_at"]) if pool_entry.get("fetched_at") else None),
            description=str(pool_entry.get("description", "")),
            owner_label=str(entry.get("label", "")).strip().lower(),
            owner_reason=str(entry.get("reason", "")).strip(),
        )
        _ = urls  # urls are folded into the Job's url list by to_job()
        cases.append(case)
    return cases


# --------------------------------------------------------------------------- #
# Pipeline replay
# --------------------------------------------------------------------------- #


def _prediction_from_processed(
    processed: Any,
    *,
    tokens: int,
    calls: int,
    profile: CandidateProfile | None = None,
    per_call: list[dict[str, Any]] | None = None,
    prompt_version: str | None = None,
    as_of: str | None = None,
    cheap: bool = False,
) -> SystemPrediction:
    """Reduce a ``ProcessedJob`` to the audit essentials for the report."""
    pred = SystemPrediction(
        outcome="skip",
        tokens=tokens,
        llm_calls=calls,
        per_call=list(per_call or []),
        prompt_version=prompt_version,
        as_of=as_of,
    )

    # --- cheap-filter rejection: zero LLM calls, definitive skip ------------- #
    filter_result = processed.filter_result
    if filter_result is not None and not filter_result.passed:
        rules = [r.rule_id for r in filter_result.rejections]
        pred.fired_rules = rules
        pred.fired_rule = rules[0] if rules else None
        pred.reasons = [f"{r.rule_id}: {r.evidence}" for r in filter_result.rejections]
        pred.vetoes = [
            {
                "rule_id": r.rule_id,
                "evidence": getattr(r, "evidence", None),
                "reason": getattr(r, "evidence", None),
                "hard": True,
            }
            for r in getattr(filter_result, "rejections", []) or []
        ]
        pred.outcome = "skip"
        pred.status = "cheap" if cheap else "incomplete"
        pred.reason = "rejected by cheap filters before any LLM stage"
        pred.cheap_status = "rejected"
        return pred

    # --- no LLM decision produced ------------------------------------------ #
    decision: Decision | None = processed.decision
    if decision is None:
        # Parse-confidence review or evidence failure: the pipeline surfaced it
        # for a human without a numeric decision.
        pred.outcome = "review" if processed.needs_review else "skip"
        pred.score = float(processed.score or 0.0)
        pred.status = "incomplete"
        pred.reason = "no decision produced (parse-confidence review or evidence failure)"
        pred.cheap_status = "passed" if cheap else ""
        return pred

    # --- real LLM decision ------------------------------------------------- #
    pred.reached_llm = True
    pred.outcome = decision.outcome.value
    pred.score = float(decision.match_score)
    pred.confidence = float(decision.confidence)
    pred.fired_rules = [veto.rule_id for veto in decision.vetoes]
    pred.fired_rule = pred.fired_rules[0] if pred.fired_rules else None
    reasons = [f"{veto.rule_id}: {veto.reason}" for veto in decision.vetoes]
    if not reasons:
        reasons = [ev.quote for ev in decision.reasons_against if ev.quote]
    pred.reasons = reasons[:5]
    pred.vetoes = [
        {
            "rule_id": getattr(v, "rule_id", None),
            "reason": getattr(v, "reason", None),
            "hard": getattr(v, "hard", None),
            "evidence": (
                getattr(getattr(v, "evidence", None), "quote", None)
                if getattr(v, "evidence", None) is not None
                else None
            ),
        }
        for v in getattr(decision, "vetoes", []) or []
    ]
    parsed = getattr(processed, "parsed", None)
    if parsed is not None:
        pred.must_have_skills = list(getattr(parsed, "must_have_skills", []) or [])
        pred.nice_to_have_skills = list(getattr(parsed, "nice_to_have_skills", []) or [])
    sm = getattr(processed, "skill_match", None)
    if sm is not None:
        pred.must_have_coverage = getattr(sm, "must_have_coverage", None)
        pred.nice_to_have_coverage = getattr(sm, "nice_to_have_coverage", None)
        must = pred.must_have_skills
        profile_skills = list(getattr(profile, "skills", []) or [])
        if must and profile_skills:
            from .skills import explain_skills

            details = list(explain_skills(must, profile_skills))
            pred.skill_details = details
            # The numerator/denominator the coverage figure is built from.
            pred.must_have_coverage_den = len(must)
            num = 0.0
            for d in details:
                c = d.get("credit") or 0.0
                if isinstance(c, (int, float)) and not isinstance(c, bool):
                    num += float(c)
            pred.must_have_coverage_num = round(num, 6)
        elif must and pred.must_have_coverage is not None:
            pred.must_have_coverage_den = len(must)
            pred.must_have_coverage_num = round(
                float(pred.must_have_coverage) * float(pred.must_have_coverage_den), 6
            )
    dims = getattr(decision, "dimensions", None) or {}
    if isinstance(dims, dict):
        pred.dimension_scores = {k: float(v) for k, v in dims.items()}

    # Status: only "evaluated" when a real LLM decision has confidence > 0 and
    # must-have data; LLM calls that fell short are "incomplete".
    if calls > 0 and pred.reached_llm:
        has_must_have_data = bool(pred.must_have_skills) or pred.must_have_coverage is not None
        if pred.confidence > 0 and has_must_have_data:
            pred.status = "evaluated"
            pred.reason = None
        else:
            pred.status = "incomplete"
            parts = []
            if pred.confidence <= 0:
                parts.append("confidence 0")
            if not has_must_have_data:
                parts.append("no must-have data")
            pred.reason = "incomplete decision: " + (
                ", ".join(parts) if parts else "no proper evaluation"
            )
    return pred


def replay_case(
    case: LabelEvalCase,
    *,
    client: LLMClient,
    profile: CandidateProfile,
    settings: Settings,
    conn: sqlite3.Connection,
    run_id: int,
    cheap_only: bool = False,
    as_of: Any = None,
) -> SystemPrediction:
    """Run the pipeline stages for one labelled job and reduce the result."""
    job = case.to_job()
    job_id, _ = upsert_job(conn, run_id, job)

    start_tokens = int(getattr(client, "tokens_used", 0) or 0)
    start_calls = int(getattr(client, "calls", 0) or 0)
    start_call_details = len(getattr(client, "call_details", []) or [])

    cheap = apply_cheap_filters(job, settings, as_of=as_of)
    if not cheap.passed:
        # Zero LLM calls: the same deterministic gate the live pipeline uses.
        cr = _ColdResult(job_id, job, cheap)
        pred = _prediction_from_processed(
            cr,
            tokens=0,
            calls=0,
            profile=profile,
            per_call=[],
            prompt_version=None,
            as_of=as_of.isoformat() if hasattr(as_of, "isoformat") else None,
            cheap=True,
        )
        pred.cheap_status = "rejected"
        return pred

    if cheap_only:
        # LLM-free stages only: the deterministic regex parse plus the cheap
        # gate. No LLM call is made, so this is free to run over the whole set.
        from .jd_parser import parse_jd_regex_only

        regex_jd = parse_jd_regex_only(job.title or "", job.description_raw or "")
        # "unknown", not "skip": the LLM stages never ran, so no decision was made.
        pred = SystemPrediction(outcome="unknown", tokens=0, llm_calls=0)
        pred.cheap_status = "passed"
        pred.fired_rule = None
        pred.reasons = ["passed cheap filters (LLM stages not run)"]
        pred.regex_min_years = regex_jd.min_years_experience
        pred.regex_max_years = regex_jd.max_years_experience
        pred.regex_title_seniority = regex_jd.title_seniority.value
        pred.regex_remote_type = regex_jd.remote_type.value
        pred.regex_years_quote = regex_jd.years_source_quote
        return pred

    try:
        processed = process_job(
            client,
            job_id,
            job,
            profile,
            settings,
            conn=conn,
            cheap_result=cheap,
        )
    except LLMError as exc:
        # Per-job LLM failure (non-daily 429, transport exhaustion, validation
        # failure): mark incomplete and continue to the next job. Daily-limit
        # pauses propagate to run_label_eval to stop the whole run.
        if isinstance(exc, GroqDailyLimitReached):
            raise
        delta_tokens = int(getattr(client, "tokens_used", 0) or 0) - start_tokens
        delta_calls = int(getattr(client, "calls", 0) or 0) - start_calls
        all_details = getattr(client, "call_details", []) or []
        per_call = list(all_details[start_call_details:])
        try:
            _parser_text, parser_version = load_prompt("jd_parser")
        except Exception:  # noqa: BLE001 - graceful fallback for prompt loading
            parser_version = None
        pred = SystemPrediction(
            outcome="skip",
            tokens=delta_tokens,
            llm_calls=delta_calls,
            status="incomplete",
            reason=f"LLM error: {exc}",
            per_call=per_call,
            prompt_version=parser_version,
            as_of=datetime.now(UTC).isoformat(),
        )
        pred.cheap_status = "passed" if cheap.passed else "rejected"
        return pred

    delta_tokens = int(getattr(client, "tokens_used", 0) or 0) - start_tokens
    delta_calls = int(getattr(client, "calls", 0) or 0) - start_calls
    # Per-call details added during this job's processing.
    all_details = getattr(client, "call_details", []) or []
    per_call = list(all_details[start_call_details:])
    # jd_parser prompt version used for this job's parse.
    try:
        _parser_text, parser_version = load_prompt("jd_parser")
    except Exception:  # noqa: BLE001 - graceful fallback for prompt loading
        parser_version = None
    eval_as_of = datetime.now(UTC).isoformat()
    pred = _prediction_from_processed(
        processed,
        tokens=delta_tokens,
        calls=delta_calls,
        profile=profile,
        per_call=per_call,
        prompt_version=parser_version,
        as_of=eval_as_of,
    )
    pred.cheap_status = "passed" if cheap.passed else "rejected"
    return pred


class _ColdResult:
    """A minimal stand-in for a ``ProcessedJob`` rejected by the cheap gate.

    Keeps :func:`replay_case` from spending an LLM call just to reuse the same
    reduction path; it exposes only the attributes that path reads.
    """

    def __init__(self, job_id: int, job: Job, filter_result: Any) -> None:
        self.job_id = job_id
        self.job = job
        self.filter_result = filter_result
        self.decision = None
        self.needs_review = False
        self.score = 0.0
        self.parsed = None
        self.skill_match = None
        self.profile = None


# --------------------------------------------------------------------------- #
# Per-job result store (gitignored): makes a run resumable
# --------------------------------------------------------------------------- #

DEFAULT_RESULTS_PATH = Path("eval") / "label_eval_results.jsonl"


def result_record(
    case: LabelEvalCase, prediction: SystemPrediction, *, mode: str = "full"
) -> dict[str, Any]:
    """One JSONL record: everything the owner asked to see for a single job.

    ``mode`` is ``"full"`` when the LLM stages ran and ``"cheap"`` when only the
    LLM-free pre-screen ran. A ``"full"`` record is what makes a job skippable
    on the next run.
    """
    return {
        "short_id": case.short_id,
        "pool_id": case.pool_id,
        "title": case.title,
        "company": case.company,
        "url": case.url,
        "source": case.source,
        "owner_label": case.owner_label,
        "owner_reason": case.owner_reason,
        "mode": mode,
        "system_decision": prediction.outcome,
        "score": prediction.score,
        "confidence": prediction.confidence,
        "fired_rules": list(prediction.fired_rules),
        "vetoes": list(prediction.vetoes),
        "reasons": list(prediction.reasons),
        "must_have_skills": list(prediction.must_have_skills),
        "nice_to_have_skills": list(prediction.nice_to_have_skills),
        "skill_details": list(prediction.skill_details),
        "missing_skills": [
            str(d.get("skill")) for d in prediction.skill_details if not d.get("matched")
        ],
        "must_have_coverage": prediction.must_have_coverage,
        "must_have_coverage_numerator": prediction.must_have_coverage_num,
        "must_have_coverage_denominator": prediction.must_have_coverage_den,
        "nice_to_have_coverage": prediction.nice_to_have_coverage,
        "dimension_scores": dict(prediction.dimension_scores),
        "llm_calls": prediction.llm_calls,
        "tokens": prediction.tokens,
        "reached_llm": prediction.reached_llm,
        "cheap_status": prediction.cheap_status,
        "regex_min_years": prediction.regex_min_years,
        "regex_max_years": prediction.regex_max_years,
        "regex_title_seniority": prediction.regex_title_seniority,
        "regex_remote_type": prediction.regex_remote_type,
        "regex_years_quote": prediction.regex_years_quote,
        "status": prediction.status,
        "reason": prediction.reason,
        "prompt_version": prediction.prompt_version,
        "as_of": prediction.as_of,
        "per_call": list(prediction.per_call),
    }


def prediction_from_record(record: dict[str, Any]) -> SystemPrediction:
    """Rebuild a :class:`SystemPrediction` from a stored result record."""
    return SystemPrediction(
        outcome=str(record.get("system_decision") or "skip"),
        score=float(record.get("score") or 0.0),
        confidence=float(record.get("confidence") or 0.0),
        fired_rule=(record.get("fired_rules") or [None])[0],
        fired_rules=list(record.get("fired_rules") or []),
        reasons=list(record.get("reasons") or []),
        llm_calls=int(record.get("llm_calls") or 0),
        tokens=int(record.get("tokens") or 0),
        reached_llm=bool(record.get("reached_llm")),
        vetoes=list(record.get("vetoes") or []),
        must_have_skills=list(record.get("must_have_skills") or []),
        nice_to_have_skills=list(record.get("nice_to_have_skills") or []),
        skill_details=list(record.get("skill_details") or []),
        must_have_coverage=record.get("must_have_coverage"),
        nice_to_have_coverage=record.get("nice_to_have_coverage"),
        must_have_coverage_num=record.get("must_have_coverage_numerator"),
        must_have_coverage_den=record.get("must_have_coverage_denominator"),
        dimension_scores=dict(record.get("dimension_scores") or {}),
        cheap_status=str(record.get("cheap_status") or ""),
        regex_min_years=record.get("regex_min_years"),
        regex_max_years=record.get("regex_max_years"),
        regex_title_seniority=str(record.get("regex_title_seniority") or ""),
        regex_remote_type=str(record.get("regex_remote_type") or ""),
        regex_years_quote=record.get("regex_years_quote"),
        status=str(record.get("status") or "incomplete"),
        reason=record.get("reason"),
        prompt_version=record.get("prompt_version"),
        as_of=record.get("as_of"),
        per_call=list(record.get("per_call") or []),
    )


def load_all_records(path: Path | str = DEFAULT_RESULTS_PATH) -> list[dict[str, Any]]:
    """Every record in the per-job results file, in file order."""
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_stored_results(
    path: Path | str = DEFAULT_RESULTS_PATH, *, mode: str = "full"
) -> dict[str, dict[str, Any]]:
    """Read the per-job results file: ``short_id -> record`` (last wins).

    Only records with the requested ``mode`` are returned, so a cheap-only
    record never makes a job look "already measured" to a full run.
    """
    out: dict[str, dict[str, Any]] = {}
    for record in load_all_records(path):
        if str(record.get("mode") or "full") != mode:
            continue
        short_id = str(record.get("short_id") or "").strip()
        if short_id:
            out[short_id] = record
    return out


def merge_records(
    existing: Sequence[dict[str, Any]],
    new: Sequence[dict[str, Any]],
    *,
    mode: str,
) -> list[dict[str, Any]]:
    """Replace the ``mode`` records in ``existing`` with ``new``, keeping the rest.

    Keeps exactly one line per (short_id, mode) so a re-run does not grow the
    file without bound, and so a cheap-only run never drops the full records.
    """
    kept = [
        record
        for record in existing
        if str(record.get("mode") or "full") != mode
        or str(record.get("short_id") or "").strip()
        not in {str(r.get("short_id") or "").strip() for r in new}
    ]
    return [*kept, *new]


def write_results(path: Path | str, records: Sequence[dict[str, Any]]) -> Path:
    """Rewrite the per-job results file with ``records`` (one JSONL line each)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    target.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return target


@dataclass
class LabelEvalReport:
    """Everything ``eval-labels`` prints and writes."""

    cases: list[CaseResult] = field(default_factory=list)
    skipped_labeled: int = 0
    remaining: int = 0
    paused_reason: str | None = None
    tokens_used: int = 0
    llm_calls: int = 0
    profile_source: str = ""
    labels_path: str = ""
    pool_path: str = ""
    cheap_only: bool = False
    resumed_from_store: int = 0
    results_path: str = ""

    @property
    def metrics(self) -> Metrics:
        return Metrics(cases=self.cases)

    @property
    def metrics_deduped(self) -> Metrics:
        return Metrics(cases=collapse_duplicates(self.cases))

    @property
    def duplicate_pairs(self) -> int:
        return len(self.cases) - len(collapse_duplicates(self.cases))

    @property
    def cheap_rejected(self) -> list[CaseResult]:
        """Cases the deterministic pre-screen rejected (zero LLM calls)."""
        return [c for c in self.cases if _cheap_status(c.prediction) == "rejected"]

    @property
    def cheap_passed(self) -> list[CaseResult]:
        """Cases that passed the pre-screen and therefore need the LLM stages."""
        return [c for c in self.cases if _cheap_status(c.prediction) == "passed"]

    @property
    def evaluated_cases(self) -> list[CaseResult]:
        """Cases with a real LLM decision (excludes incomplete jobs)."""
        return [c for c in self.cases if c.prediction.status != "incomplete"]

    @property
    def incomplete_cases(self) -> list[CaseResult]:
        """Jobs that made LLM calls but produced no proper evaluation, with reasons."""
        return [c for c in self.cases if c.prediction.status == "incomplete"]

    @property
    def metrics_evaluated(self) -> Metrics:
        """Headline metrics over evaluated jobs only (excludes incomplete)."""
        return Metrics(cases=self.evaluated_cases)


def run_label_eval(
    *,
    labels_path: Path | str = DEFAULT_LABELS_PATH,
    pool_path: Path | str = DEFAULT_POOL_PATH,
    client: LLMClient | None = None,
    settings: Settings | None = None,
    profile: CandidateProfile | None = None,
    database: Path | str | None = None,
    max_calls: int | None = None,
    max_tokens: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    cheap_only: bool = False,
    results_path: Path | str | None = None,
    resume: bool = True,
    only_apply: bool = False,
    limit: int | None = None,
    include_duplicates: bool = False,
    usage_db: Path | str | None = None,
) -> LabelEvalReport:
    """Replay the pipeline over labelled pool jobs and build the report.

    Uses a private temporary database unless ``database`` is given; the real
    ``atlas.db`` is only ever read, and only to fetch the approved profile.

    The measurement is bounded by the **Groq daily-limit guard** rather than the
    small per-product-run work cap: cheap rejections cost nothing, survivors run
    through the LLM stages, and the run stops cleanly the moment the provider
    reports the daily limit (or an optional ``max_calls``/``max_tokens`` budget is
    exhausted), leaving :attr:`LabelEvalReport.remaining` jobs unmeasured.

    Ordering: owner-apply jobs first, then owner-skip jobs that pass the cheap
    filters (LLM-free gate), then owner-skip jobs rejected by the cheap filters.
    ``only_apply`` restricts to owner-apply jobs; ``limit`` caps the number
    processed; ``include_duplicates`` keeps duplicate postings (collapsed by
    normalized company + title when off).
    """
    settings = settings or get_settings()
    cases = load_label_cases(labels_path, pool_path)
    report = LabelEvalReport(
        labels_path=str(labels_path),
        pool_path=str(pool_path),
        profile_source="provided" if profile is not None else "",
        cheap_only=cheap_only,
        results_path=str(results_path) if results_path else "",
    )

    # --- ordering: owner-apply first, then owner-skip passing cheap filters -- #
    apply_cases = [c for c in cases if c.owner_label == "apply"]
    skip_cases = [c for c in cases if c.owner_label == "skip"]
    skip_passing: list[LabelEvalCase] = []
    skip_failing: list[LabelEvalCase] = []
    for case in skip_cases:
        _cheap = apply_cheap_filters(case.to_job(), settings)
        (skip_passing if _cheap.passed else skip_failing).append(case)
    ordered_cases = apply_cases + skip_passing + skip_failing
    if only_apply:
        ordered_cases = list(apply_cases)
    if not include_duplicates:
        seen: set[tuple[str, str]] = set()
        deduped: list[LabelEvalCase] = []
        for case in ordered_cases:
            key = duplicate_key(case)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(case)
        ordered_cases = deduped
    if limit is not None:
        ordered_cases = ordered_cases[: int(limit)]

    current_version = None
    if not cheap_only:
        try:
            _parser_text, current_version = load_prompt("jd_parser")
        except Exception:  # noqa: BLE001 - graceful fallback for prompt loading
            current_version = None

    stored: dict[str, dict[str, Any]] = {}
    if results_path is not None and resume:
        stored = load_stored_results(results_path, mode="full")

    tmpdir: tempfile.TemporaryDirectory[str] | None = None
    if database is None:
        tmpdir = tempfile.TemporaryDirectory(prefix="atlas-eval-labels-")
        db_path: Path | str = Path(tmpdir.name) / "eval_labels.db"
    else:
        db_path = database

    conn: sqlite3.Connection | None = None
    try:
        conn = connect(db_path)
        init_db(conn)

        if profile is None:
            profile, source = _load_approved_profile()
            report.profile_source = source
        if profile is None:
            report.paused_reason = "no approved profile available"
            return report

        run_id = _start_eval_run(conn)

        daily_cap = max_tokens if max_tokens is not None else settings.run.max_llm_tokens_per_day
        inner: Any = client if client is not None else _NullClient()
        budgeted = cast(LLMClient, _BudgetedClient(inner, max_calls, daily_cap))

        tokens_before = int(getattr(budgeted, "tokens_used", 0) or 0)
        calls_before = int(getattr(budgeted, "calls", 0) or 0)

        mode = "cheap" if cheap_only else "full"
        new_records: list[dict[str, Any]] = []
        for index, case in enumerate(ordered_cases):
            if on_progress is not None:
                on_progress({"done": index, "total": len(ordered_cases), "current": case.short_id})
            stored_record = stored.get(case.short_id)
            if stored_record is not None and not cheap_only:
                # Skip only evaluated records built with the current jd_parser
                # prompt version; re-run incomplete or stale-version records.
                status = str(stored_record.get("status") or "incomplete")
                pver = stored_record.get("prompt_version")
                if status == "evaluated" and pver == current_version:
                    prediction = prediction_from_record(stored_record)
                    report.cases.append(CaseResult(case=case, prediction=prediction))
                    report.resumed_from_store += 1
                    new_records.append(stored_record)
                    continue
            try:
                prediction = replay_case(
                    case,
                    client=budgeted,
                    profile=profile,
                    settings=settings,
                    conn=conn,
                    run_id=run_id,
                    cheap_only=cheap_only,
                )
            except GroqDailyLimitReached as exc:
                report.paused_reason = f"daily limit: {exc}"
                report.remaining = len(ordered_cases) - index
                break
            except (LLMTokenBudgetExceeded, LLMCallBudgetExceeded) as exc:
                report.paused_reason = f"budget cap: {exc}"
                report.remaining = len(ordered_cases) - index
                break
            report.cases.append(CaseResult(case=case, prediction=prediction))
            new_records.append(result_record(case, prediction, mode=mode))

        if results_path is not None and new_records:
            existing = load_all_records(results_path)
            write_results(results_path, merge_records(existing, new_records, mode=mode))

        report.tokens_used = int(getattr(budgeted, "tokens_used", 0) or 0) - tokens_before
        report.llm_calls = int(getattr(budgeted, "calls", 0) or 0) - calls_before
        report.skipped_labeled = len(cases) - len(report.cases)

        # Record tokens spent against the real local daily-budget table.
        if usage_db is not None and report.tokens_used > 0:
            real_conn = connect(usage_db)
            try:
                init_db(real_conn)
                add_llm_tokens(real_conn, report.tokens_used)
            finally:
                real_conn.close()
        return report
    finally:
        if conn is not None:
            conn.close()
        if tmpdir is not None:
            tmpdir.cleanup()


class _NullClient:
    """A client that refuses every call, for a run that must spend no tokens.

    The cheap-only run never reaches an LLM stage, so it must not need a
    provider key at all: it still needs a client-shaped object for the budget
    wrapper to count against, so this one counts and then refuses.
    """

    def __init__(self) -> None:
        self.tokens_used = 0
        self.calls = 0
        self.on_wait = None

    def call_json(self, *args: Any, **kwargs: Any) -> Any:
        raise LLMError("this run must not make an LLM call")


def _load_approved_profile() -> tuple[CandidateProfile | None, str]:
    """Read the latest profile from the real DB, read-only.

    The replay writes only to a private temporary database, so the profile must
    come from the real ``atlas.db`` (``ATLAS_DB`` when set) - never from the
    empty temp copy.
    """
    real = Path(db_path()).resolve()
    if not real.exists():
        return None, ""
    try:
        conn = sqlite3.connect(real.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None, ""
    try:
        profile = get_latest_profile(conn)
    except sqlite3.Error:
        profile = None
    finally:
        conn.close()
    if profile is not None and profile.approved:
        return profile, "real atlas.db (latest approved profile)"
    return profile, "real atlas.db (latest profile)" if profile is not None else ""


def _start_eval_run(conn: sqlite3.Connection) -> int:
    from .db import start_run

    return start_run(conn, "eval-labels")


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def format_cheap_report(report: LabelEvalReport) -> str:
    """Render the cheap-filter-only report: counts, never an LLM call.

    Shows how many labelled jobs the deterministic pre-screen rejects (and by
    which rule), split by the owner's label, and lists every owner-apply job
    the pre-screen rejects - those are the silent false negatives.
    """
    lines: list[str] = []
    lines.append("# Cheap filters vs owner labels (no LLM calls)")
    lines.append("")
    lines.append(f"- labels: `{report.labels_path}`")
    lines.append(f"- pool: `{report.pool_path}`")
    lines.append(f"- labelled jobs evaluated: {len(report.cases)}")
    lines.append(f"- LLM calls: {report.llm_calls}   tokens: {report.tokens_used:,}")
    lines.append("")

    rejected = report.cheap_rejected
    passed = report.cheap_passed

    lines.append("## Counts by owner label")
    lines.append("")
    lines.append("| owner label | rejected by cheap filters | passed (needs LLM) | total |")
    lines.append("|---|---|---|---|")
    for label in ("apply", "skip"):
        rej = [c for c in rejected if c.case.owner_label == label]
        pas = [c for c in passed if c.case.owner_label == label]
        lines.append(f"| {label} | {len(rej)} | {len(pas)} | {len(rej) + len(pas)} |")
    lines.append(
        f"| **total** | **{len(rejected)}** | **{len(passed)}** | **{len(rejected) + len(passed)}** |"
    )
    lines.append("")

    lines.append("## Rejections by rule")
    lines.append("")
    if rejected:
        by_rule: dict[str, list[CaseResult]] = {}
        for case in rejected:
            for rule in case.prediction.fired_rules:
                by_rule.setdefault(rule, []).append(case)
        for rule, cases in sorted(by_rule.items(), key=lambda item: (-len(item[1]), item[0])):
            owner_apply = sum(1 for c in cases if c.case.owner_label == "apply")
            lines.append(f"- `{rule}`: {len(cases)} (owner apply: {owner_apply})")
    else:
        lines.append("- (no rejections)")
    lines.append("")

    lines.append("## Owner-apply jobs rejected by a cheap filter")
    lines.append("")
    apply_rejected = [c for c in rejected if c.case.owner_label == "apply"]
    if apply_rejected:
        for case in apply_rejected:
            c = case.case
            lines.append(f"### {c.short_id} - {c.title}")
            lines.append("")
            lines.append(f"- owner: **{c.owner_label.upper()}** - {c.owner_reason}")
            lines.append(f"- company: {c.company}   source: {c.source}")
            lines.append(f"- url: {c.url}")
            for reason in case.prediction.reasons:
                lines.append(f"- {reason}")
            lines.append("")
    else:
        lines.append("(none)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _matrix_rows(metrics: Metrics) -> list[str]:
    apply_row = (
        f"| owner apply | {metrics.owner_apply_system_apply} | "
        f"{metrics.owner_apply_system_review} | {metrics.owner_apply_system_skip} | "
        f"{metrics.owner_apply_total} |"
    )
    skip_row = (
        f"| owner skip | {metrics.owner_skip_system_apply} | "
        f"{metrics.owner_skip_system_review} | {metrics.owner_skip_system_skip} | "
        f"{metrics.owner_skip_total} |"
    )
    total_row = (
        f"| total | {metrics.system_apply_total} | "
        f"{metrics.owner_apply_system_review + metrics.owner_skip_system_review} | "
        f"{metrics.owner_apply_system_skip + metrics.owner_skip_system_skip} | "
        f"{len(metrics.cases)} |"
    )
    return [
        "| owner \\ system | APPLY | REVIEW | SKIP | total |",
        "|---|---|---|---|---|",
        apply_row,
        skip_row,
        total_row,
    ]


def format_report(report: LabelEvalReport) -> str:
    """Render the full report as Markdown (also the console output)."""
    metrics = report.metrics_evaluated
    deduped = Metrics(cases=collapse_duplicates(report.evaluated_cases))
    lines: list[str] = []
    lines.append("# Pipeline vs owner labels")
    lines.append("")
    lines.append(f"- labels: `{report.labels_path}`")
    lines.append(f"- pool: `{report.pool_path}`")
    lines.append(f"- profile: {report.profile_source or 'n/a'}")
    lines.append(f"- labelled jobs loaded: {len(report.cases)}")
    lines.append(f"- evaluated (confidence > 0, must-have data): {len(report.evaluated_cases)}")
    lines.append(f"- incomplete (LLM calls, no proper evaluation): {len(report.incomplete_cases)}")
    if report.duplicate_pairs:
        lines.append(f"- duplicate postings collapsed: {report.duplicate_pairs}")
    lines.append(f"- LLM calls: {report.llm_calls}   tokens: {report.tokens_used:,}")
    if report.resumed_from_store:
        lines.append(
            f"- reused from results store: {report.resumed_from_store} job(s) (no tokens spent)"
        )
    if report.paused_reason:
        lines.append(f"- STOPPED: {report.paused_reason}; jobs remaining: {report.remaining}")
    lines.append("")

    lines.append("## Confusion matrix (evaluated jobs only)")
    lines.append("")
    lines.extend(_matrix_rows(metrics))
    lines.append("")

    lines.append("## Headline figures (with sample size and 95% Wilson interval)")
    lines.append("")
    lines.append(
        f"- precision of APPLY: "
        f"{_pct_ci(metrics.precision_apply_numerator, metrics.system_apply_total)}"
    )
    lines.append(
        f"- recall of owner-apply jobs (APPLY+REVIEW): "
        f"{_pct_ci(metrics.recall_apply_numerator, metrics.owner_apply_total)}"
    )
    false_pass = metrics.seniority_false_pass
    lines.append(
        f"- seniority false-pass (owner skip w/ seniority reason -> APPLY/REVIEW): "
        f"{_pct_ci(len(false_pass), metrics.owner_skip_total)}"
    )
    lines.append("")

    lines.append("## Excluding duplicate postings (collapsed by company + title)")
    lines.append("")
    lines.extend(_matrix_rows(deduped))
    lines.append("")
    lines.append(
        f"- precision of APPLY: "
        f"{_pct_ci(deduped.precision_apply_numerator, deduped.system_apply_total)}"
    )
    lines.append(
        f"- recall of owner-apply jobs (APPLY+REVIEW): "
        f"{_pct_ci(deduped.recall_apply_numerator, deduped.owner_apply_total)}"
    )
    lines.append(
        f"- seniority false-pass: "
        f"{_pct_ci(len(deduped.seniority_false_pass), deduped.owner_skip_total)}"
    )
    lines.append("")

    lines.append("## Per job")
    lines.append("")
    lines.append(
        "| short id | title | company | owner | system | status | score | conf | rules | calls | tokens |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for case in report.cases:
        c = case.case
        p = case.prediction
        rules = ", ".join(p.fired_rules) if p.fired_rules else "-"
        lines.append(
            f"| {c.short_id} | {c.title} | {c.company} | {c.owner_label} | "
            f"{p.outcome.upper()} | {p.status} | {p.score:.1f} | {p.confidence:.2f} | {rules} | "
            f"{p.llm_calls} | {p.tokens} |"
        )
    lines.append("")

    # --- incomplete jobs with reasons -------------------------------------- #
    if report.incomplete_cases:
        lines.append("## Incomplete jobs (excluded from metrics)")
        lines.append("")
        lines.append("| short id | title | company | owner | llm_calls | tokens | reason |")
        lines.append("|---|---|---|---|---|---|---|")
        for case in report.incomplete_cases:
            c = case.case
            p = case.prediction
            reason = (p.reason or "").replace("|", "\\|")
            lines.append(
                f"| {c.short_id} | {c.title} | {c.company} | {c.owner_label} | "
                f"{p.llm_calls} | {p.tokens} | {reason} |"
            )
        lines.append("")

    # --- per-source and per-rule (evaluated jobs) -------------------------- #
    lines.append("## Per source")
    lines.append("")
    lines.append("| source | n | owner apply | system APPLY | precision APPLY | recall APPLY |")
    lines.append("|---|---|---|---|---|---|")
    for source, sm in metrics.per_source().items():
        lines.append(
            f"| {source} | {len(sm.cases)} | {sm.owner_apply_total} | {sm.system_apply_total} "
            f"| {_pct_ci(sm.precision_apply_numerator, sm.system_apply_total)} "
            f"| {_pct_ci(sm.recall_apply_numerator, sm.owner_apply_total)} |"
        )
    lines.append("")

    lines.append("## Per rule (how often each filter/veto fired)")
    lines.append("")
    per_rule = metrics.per_rule()
    if per_rule:
        for rule, count in per_rule.items():
            lines.append(f"- `{rule}`: {count}")
    else:
        lines.append("- (no rules fired)")
    lines.append("")

    # --- cost report: per-stage aggregation -------------------------------- #
    stage_calls: dict[str, int] = {}
    stage_tokens: dict[str, int] = {}
    for case in report.cases:
        for call in case.prediction.per_call:
            stage = str(call.get("stage") or "unknown")
            stage_calls[stage] = stage_calls.get(stage, 0) + 1
            stage_tokens[stage] = stage_tokens.get(stage, 0) + int(call.get("tokens") or 0)
    if stage_calls:
        lines.append("## Cost report (tokens and calls per LLM stage)")
        lines.append("")
        lines.append("| stage | calls | tokens |")
        lines.append("|---|---|---|")
        for stage in sorted(stage_calls):
            lines.append(f"| {stage} | {stage_calls[stage]} | {stage_tokens[stage]} |")
        lines.append("")

    # --- per-job retries / validation failures ----------------------------- #
    lines.append("## Per-job LLM call details (retries, validation failures, errors)")
    lines.append("")
    lines.append(
        "| short id | stage | model | tokens | transport_retries | validation_attempts | validation_failures | http_errors | ok | error |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for case in report.cases:
        c = case.case
        for call in case.prediction.per_call:
            errors = call.get("http_errors") or []
            lines.append(
                f"| {c.short_id} | {call.get('stage')} | {call.get('model')} | "
                f"{call.get('tokens', 0)} | {call.get('transport_retries', 0)} | "
                f"{call.get('validation_attempts', 0)} | {call.get('validation_failures', 0)} | "
                f"{','.join(str(e) for e in errors)} | {call.get('ok', False)} | "
                f"{_esc(call.get('error') or '')} |"
            )
    lines.append("")

    lines.append("## Disagreements")
    lines.append("")
    disagreements = metrics.disagreements()
    lines.append(f"{len(disagreements)} disagreement(s).")
    lines.append("")
    for case in disagreements:
        c = case.case
        p = case.prediction
        lines.append(f"### {c.short_id} - {c.title}")
        lines.append("")
        lines.append(f"- owner: **{c.owner_label.upper()}** - {c.owner_reason}")
        lines.append(
            f"- system: **{p.outcome.upper()}** (score {p.score:.1f}, confidence {p.confidence:.2f})"
        )
        lines.append(f"- company: {c.company}   source: {c.source}")
        lines.append(f"- url: {c.url}")
        lines.append(f"- fired rule: {p.fired_rule or '(none)'}")
        if p.reasons:
            for reason in p.reasons:
                lines.append(f"  - {reason}")
        if p.vetoes:
            lines.append("- vetoes:")
            for veto in p.vetoes:
                lines.append(
                    f"  - `{veto.get('rule_id')}` (hard={veto.get('hard')}): "
                    f"{veto.get('reason') or ''}"
                )
        if p.must_have_skills:
            lines.append(f"- must-have skills: {', '.join(p.must_have_skills)}")
            if p.must_have_coverage is not None:
                lines.append(
                    f"- must-have coverage: {p.must_have_coverage:.2f} "
                    f"({p.must_have_coverage_num:g}/{p.must_have_coverage_den})"
                )
        if p.skill_details:
            lines.append("- per-skill credit:")
            for detail in p.skill_details:
                lines.append(
                    f"  - {detail.get('skill')}: credit {detail.get('credit')} "
                    f"({detail.get('why')})"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: LabelEvalReport, path: Path | str = DEFAULT_REPORT_PATH) -> Path:
    """Write the Markdown report (gitignored: it contains job text)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = format_cheap_report(report) if report.cheap_only else format_report(report)
    target.write_text(text, encoding="utf-8")
    return target


def summary_line(report: LabelEvalReport) -> str:
    """One-line headline for HANDOFF.md."""
    m = report.metrics_evaluated
    d = Metrics(cases=collapse_duplicates(report.evaluated_cases))
    return (
        f"{len(report.evaluated_cases)} evaluated jobs; "
        f"precision APPLY {_pct_ci(m.precision_apply_numerator, m.system_apply_total)}; "
        f"recall owner-apply {_pct_ci(m.recall_apply_numerator, m.owner_apply_total)}; "
        f"seniority false-pass {len(m.seniority_false_pass)}; "
        f"incomplete {len(report.incomplete_cases)}; deduped n={len(d.cases)}"
    )
