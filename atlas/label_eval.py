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

import math
import re
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from .config import Settings, get_settings
from .db import connect, db_path, get_latest_profile, init_db, upsert_job
from .filters import apply_cheap_filters
from .llm import GroqDailyLimitReached, LLMClient
from .pipeline import (
    LLMCallBudgetExceeded,
    LLMTokenBudgetExceeded,
    _BudgetedClient,
    process_job,
)
from .schemas import CandidateProfile, Decision, Job, RemoteType

DEFAULT_LABELS_PATH = Path("eval") / "labels.jsonl"
DEFAULT_POOL_PATH = Path("eval") / "labeling_pool.jsonl"
DEFAULT_REPORT_PATH = Path("eval") / "label_eval_report.md"


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


def _prediction_from_processed(processed: Any, *, tokens: int, calls: int) -> SystemPrediction:
    """Reduce a ``ProcessedJob`` to the audit essentials for the report."""
    pred = SystemPrediction(outcome="skip", tokens=tokens, llm_calls=calls)

    filter_result = processed.filter_result
    if filter_result is not None and not filter_result.passed:
        rules = [r.rule_id for r in filter_result.rejections]
        pred.fired_rules = rules
        pred.fired_rule = rules[0] if rules else None
        pred.reasons = [f"{r.rule_id}: {r.evidence}" for r in filter_result.rejections]
        pred.outcome = "skip"
        return pred

    decision: Decision | None = processed.decision
    if decision is None:
        # Parse-confidence review or evidence failure: the pipeline surfaced it
        # for a human without a numeric decision.
        pred.outcome = "review" if processed.needs_review else "skip"
        pred.score = float(processed.score or 0.0)
        return pred

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
    return pred


def replay_case(
    case: LabelEvalCase,
    *,
    client: LLMClient,
    profile: CandidateProfile,
    settings: Settings,
    conn: sqlite3.Connection,
    run_id: int,
) -> SystemPrediction:
    """Run the pipeline stages for one labelled job and reduce the result."""
    job = case.to_job()
    job_id, _ = upsert_job(conn, run_id, job)

    start_tokens = int(getattr(client, "tokens_used", 0) or 0)
    start_calls = int(getattr(client, "calls", 0) or 0)

    cheap = apply_cheap_filters(job, settings)
    if not cheap.passed:
        # Zero LLM calls: the same deterministic gate the live pipeline uses.
        return _prediction_from_processed(_ColdResult(job_id, job, cheap), tokens=0, calls=0)

    processed = process_job(
        client,
        job_id,
        job,
        profile,
        settings,
        conn=conn,
        cheap_result=cheap,
    )
    delta_tokens = int(getattr(client, "tokens_used", 0) or 0) - start_tokens
    delta_calls = int(getattr(client, "calls", 0) or 0) - start_calls
    return _prediction_from_processed(processed, tokens=delta_tokens, calls=delta_calls)


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

    @property
    def metrics(self) -> Metrics:
        return Metrics(cases=self.cases)

    @property
    def metrics_deduped(self) -> Metrics:
        return Metrics(cases=collapse_duplicates(self.cases))

    @property
    def duplicate_pairs(self) -> int:
        return len(self.cases) - len(collapse_duplicates(self.cases))


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
) -> LabelEvalReport:
    """Replay the pipeline over labelled pool jobs and build the report.

    Uses a private temporary database unless ``database`` is given; the real
    ``atlas.db`` is only ever read, and only to fetch the approved profile.

    The measurement is bounded by the **Groq daily-limit guard** rather than the
    small per-product-run work cap: cheap rejections cost nothing, survivors run
    through the LLM stages, and the run stops cleanly the moment the provider
    reports the daily limit (or an optional ``max_calls``/``max_tokens`` budget is
    exhausted), leaving :attr:`LabelEvalReport.remaining` jobs unmeasured.
    """
    settings = settings or get_settings()
    cases = load_label_cases(labels_path, pool_path)
    report = LabelEvalReport(
        labels_path=str(labels_path),
        pool_path=str(pool_path),
        profile_source="provided" if profile is not None else "",
    )

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
        budgeted = cast(LLMClient, _BudgetedClient(client or _null_client(), max_calls, daily_cap))

        tokens_before = int(getattr(budgeted, "tokens_used", 0) or 0)
        calls_before = int(getattr(budgeted, "calls", 0) or 0)

        for index, case in enumerate(cases):
            if on_progress is not None:
                on_progress({"done": index, "total": len(cases), "current": case.short_id})
            try:
                prediction = replay_case(
                    case,
                    client=budgeted,
                    profile=profile,
                    settings=settings,
                    conn=conn,
                    run_id=run_id,
                )
            except GroqDailyLimitReached as exc:
                report.paused_reason = f"daily limit: {exc}"
                report.remaining = len(cases) - index
                break
            except (LLMTokenBudgetExceeded, LLMCallBudgetExceeded) as exc:
                report.paused_reason = f"budget cap: {exc}"
                report.remaining = len(cases) - index
                break
            report.cases.append(CaseResult(case=case, prediction=prediction))

        report.tokens_used = int(getattr(budgeted, "tokens_used", 0) or 0) - tokens_before
        report.llm_calls = int(getattr(budgeted, "calls", 0) or 0) - calls_before
        report.skipped_labeled = len(cases) - len(report.cases)
        return report
    finally:
        if conn is not None:
            conn.close()
        if tmpdir is not None:
            tmpdir.cleanup()


def _null_client() -> LLMClient:
    """A client with no API key; only ever used when no LLM call is needed."""
    from .llm import LLMClient as _LLMClient

    return _LLMClient(api_key=None)


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
    metrics = report.metrics
    deduped = report.metrics_deduped
    lines: list[str] = []
    lines.append("# Pipeline vs owner labels")
    lines.append("")
    lines.append(f"- labels: `{report.labels_path}`")
    lines.append(f"- pool: `{report.pool_path}`")
    lines.append(f"- profile: {report.profile_source or 'n/a'}")
    lines.append(f"- labelled jobs evaluated: {len(report.cases)}")
    if report.duplicate_pairs:
        lines.append(f"- duplicate postings collapsed: {report.duplicate_pairs}")
    lines.append(f"- LLM calls: {report.llm_calls}   tokens: {report.tokens_used:,}")
    if report.paused_reason:
        lines.append(f"- STOPPED: {report.paused_reason}; jobs remaining: {report.remaining}")
    lines.append("")

    lines.append("## Confusion matrix (all labelled jobs)")
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
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: LabelEvalReport, path: Path | str = DEFAULT_REPORT_PATH) -> Path:
    """Write the Markdown report (gitignored: it contains job text)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(format_report(report), encoding="utf-8")
    return target


def summary_line(report: LabelEvalReport) -> str:
    """One-line headline for HANDOFF.md."""
    m = report.metrics
    d = report.metrics_deduped
    return (
        f"{len(report.cases)} labelled jobs; "
        f"precision APPLY {_pct_ci(m.precision_apply_numerator, m.system_apply_total)}; "
        f"recall owner-apply {_pct_ci(m.recall_apply_numerator, m.owner_apply_total)}; "
        f"seniority false-pass {len(m.seniority_false_pass)}; "
        f"deduped n={len(d.cases)}"
    )
