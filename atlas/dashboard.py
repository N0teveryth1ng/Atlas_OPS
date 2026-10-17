"""Local web dashboard for Atlas: setup, profile approval, and matched jobs.

Run with ``python -m atlas.cli dashboard`` (or ``uvicorn app:app``). This is a
thin, server-rendered layer over the existing CLI pipeline: it uploads a resume,
calls the same profile builder, stores an approved :class:`CandidateProfile`,
and can kick off the existing pipeline in a background thread.

Everything is manual bookkeeping. The dashboard never applies to a job: the
``APLD`` / ``NTAPLD`` / ``PEND`` buttons only store what a human clicked.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from .config import get_settings
from .db import (
    APPLICATION_STATUSES,
    OWNER_VERDICT_CHOICES,
    connect,
    fail_stale_runs,
    finish_run,
    get_latest_profile,
    init_db,
    latest_kit_data,
    latest_owner_verdict,
    latest_run,
    matched_jobs,
    rejected_jobs,
    save_kit,
    save_owner_verdict,
    save_profile,
    set_application_status,
    shippable_job,
    shippable_jobs,
    start_run,
    tokens_used_today,
    upsert_job,
)
from .digest import is_http_url
from .kit import ApplicationKit, generate_kit, render_kit_text
from .llm import GroqDailyLimitReached, LLMClient, LLMError
from .profile_agent import build_profile as _build_profile_impl
from .profile_agent import normalize_skill_name
from .schemas import (
    CandidateProfile,
    Education,
    EvidenceSource,
    Experience,
    ExperienceLevel,
    Job,
    Links,
    Proficiency,
    Project,
    Seniority,
    Skill,
)

_LABELS = {"APLD": "Applied", "NTAPLD": "Not applied", "PEND": "Pending"}
_COLORS = {"APLD": "#1a7f37", "NTAPLD": "#cf222e", "PEND": "#9a6700"}
_MIN_SKILL_WARNING = 5
#: Outcomes the results table may show; SKIP is decided but never shown here.
_SHOWN_OUTCOMES = frozenset({"apply", "review"})
#: Any outcome the decision engine can store (the per-job page shows all three).
_DECISION_OUTCOMES = frozenset({"apply", "review", "skip"})
#: Non-real rows (test fixtures inserted by the pipeline tests) kept off the page.
_TEST_SOURCES = frozenset({"manual"})

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 5
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
#: Cap on a pasted job description; the owner pastes text, never a URL fetch.
MAX_JD_CHARS = 20000
_SESSION_TTL = 3600 * 24 * 7

logger = logging.getLogger(__name__)

_pipeline_lock = threading.Lock()
_sessions: dict[str, float] = {}


def _blank_pipeline_status() -> dict[str, Any]:
    return {
        "running": False,
        "stage": "idle",
        "counts": {},
        "stages": {},
        "sources": [],
        "progress": {},
        "errors": {},
        "current_source": None,
        "started_at": None,
        "finished_at": None,
        "result": None,
        # Cleared on every new run so a live run never shows the stale
        # "restored run" banner from a previous process.
        "recovered_run_id": None,
    }


_pipeline_status: dict[str, Any] = _blank_pipeline_status()


def _blank_add_state() -> dict[str, Any]:
    return {
        "running": False,
        "done": False,
        "job_id": None,
        "duplicate": False,
        "paused": None,
        "result_body": None,
        "progress": {},
        "error": None,
        "started_at": None,
        "finished_at": None,
    }


_add_state: dict[str, Any] = _blank_add_state()
_add_lock = threading.Lock()

_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas — dashboard</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1f2328; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 0.45rem 0.6rem; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; }
a { color: #0969da; }
.badge { color: #fff; border: 0; border-radius: 999px; padding: 0.15rem 0.6rem;
         font-size: 0.8rem; display: inline-block; }
.actions form { display: inline-block; margin-right: 0.25rem; }
button { cursor: pointer; }
button.badge { opacity: 0.5; }
button.badge:hover { opacity: 1; }
.notice { background: #fff8c5; border: 1px solid #d4a72c; padding: 0.75rem; }
.error { background: #ffeef0; border: 1px solid #cf222e; padding: 0.75rem; }
.warn { background: #fff1e5; border: 1px solid #d4691d; padding: 0.1rem 0.4rem;
        font-size: 0.75rem; display: inline-block; }
h2 { margin-top: 2rem; }
nav a { margin-right: 1rem; }
textarea, input[type=text], input[type=password] { width: 100%; box-sizing: border-box; }
</style>
</head>
<body>
"""

_TABLE_FOOT = """</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def _get_password() -> str | None:
    value = os.environ.get("DASHBOARD_PASSWORD")
    return value or None


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _check_auth(request: Request) -> bool:
    session = request.cookies.get("atlas_session")
    if session and session in _sessions:
        age = time.time() - _sessions[session]
        if age <= _SESSION_TTL:
            return True
        del _sessions[session]
    return False


def _nav() -> str:
    return (
        '<nav><a href="/">Home</a><a href="/add">Add job</a><a href="/setup">Setup</a>'
        '<a href="/profile">Profile</a><a href="/rejected">Rejected</a>'
        '<a href="/status">Status</a></nav>'
    )


def _login_form(error: str | None = None) -> str:
    err = f'<p class="error">{html.escape(error)}</p>\n' if error else ""
    return (
        _HEAD
        + "<h1>Atlas — login</h1>\n"
        + err
        + '<form method="post" action="/login">'
        + '<p><input type="password" name="password" placeholder="Password" required></p>'
        + '<button type="submit">Log in</button></form>'
        + _TABLE_FOOT
    )


def _login_page(error: str | None = None) -> str:
    err = f'<p class="error">{html.escape(error)}</p>\n' if error else ""
    return (
        _HEAD
        + "<h1>Atlas — login</h1>\n"
        + err
        + '<form method="post" action="/login">'
        + '<input type="password" name="password" placeholder="Password" required>'
        + '<button type="submit">Login</button></form>'
        + _TABLE_FOOT
    )


# --------------------------------------------------------------------------- #
# Profile building (thin wrapper so the UI and tests share one entry point)
# --------------------------------------------------------------------------- #


def build_profile(
    *,
    resume_path: str | Path | None = None,
    description: str | None = None,
    database: Path | None = None,
) -> CandidateProfile:
    """Build a profile from an uploaded resume and/or description, then store it.

    This mirrors ``atlas profile`` but is callable from the web UI. It never
    sends resume text to Braintrust: observability redaction is handled by the
    LLM client itself.
    """
    from .cli import _make_client

    resume_source = Path(resume_path) if resume_path else None
    client = _make_client()
    profile = _build_profile_impl(client, resume_source=resume_source, description=description)
    conn = connect(database)
    init_db(conn)
    try:
        save_profile(conn, profile)
    finally:
        conn.close()
    return profile


# --------------------------------------------------------------------------- #
# Background pipeline
# --------------------------------------------------------------------------- #


def _db_counts(database: Path | None) -> dict[str, int]:
    conn = connect(database)
    try:

        def count(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0])

        return {
            "jobs": count("SELECT COUNT(*) FROM jobs"),
            "passed_filters": count(
                "SELECT COUNT(DISTINCT job_id) FROM filter_results WHERE passed = 1"
            ),
            "rejected": count("SELECT COUNT(DISTINCT job_id) FROM filter_results WHERE passed = 0"),
            "decisions": count("SELECT COUNT(*) FROM decisions"),
        }
    finally:
        conn.close()


def _reset_hours_from_midnight(now: datetime) -> float:
    """Hours until the next UTC midnight, when Groq's daily quota resets."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ((midnight + timedelta(days=1)) - now).total_seconds() / 3600.0


def groq_daily_limit_message(
    database: Path | None = None, *, now: datetime | None = None
) -> str | None:
    """Why "Find jobs" must refuse to start now, or ``None`` when it may proceed.

    Two triggers: today's tracked token usage has reached 90% of the configured
    daily budget, or the most recent run paused on Groq's daily limit and its
    reset window has not elapsed yet.
    """
    now = now or datetime.now(UTC)
    limit = get_settings().run.max_llm_tokens_per_day
    try:
        conn = _open(database)
    except sqlite3.Error:
        return None
    try:
        used = tokens_used_today(conn)
        row = latest_run(conn)
    finally:
        conn.close()

    hours = _reset_hours_from_midnight(now)
    if limit > 0 and used >= 0.9 * limit:
        return f"Groq daily limit reached, resets in about {hours:.1f}h"

    if row is not None and row["status"] == "paused":
        summary = _load_json(row["summary_json"])
        if summary.get("pause_reason") == "daily":
            reset_seconds = summary.get("pause_reset_seconds")
            finished = row["finished_at"]
            if reset_seconds and finished:
                try:
                    finished_at = datetime.fromisoformat(str(finished))
                except ValueError:
                    finished_at = None
                if finished_at is not None:
                    remaining = (
                        finished_at + timedelta(seconds=float(reset_seconds)) - now
                    ).total_seconds() / 3600.0
                    if remaining > 0:
                        return f"Groq daily limit reached, resets in about {remaining:.1f}h"
                    return None
            # A daily pause with no known reset window is treated as still active.
            return f"Groq daily limit reached, resets in about {hours:.1f}h"
    return None


def _run_pipeline_worker(database: Path | None = None) -> None:
    """Collect and process jobs using the exact CLI run path, reporting progress.

    The worker never holds ``_pipeline_lock`` (that lock only guards the
    running-check in :func:`start_pipeline`), catches every failure so a dead
    thread cannot leave the page stuck, and always clears ``running`` in a
    ``finally`` block. Per-stage and per-source results land in shared state as
    the run goes, so ``/status`` moves without waiting for the run to finish.
    """
    try:
        conn = connect(database)
        init_db(conn)
        profile = get_latest_profile(conn)
        conn.close()
        if profile is None or not profile.approved:
            _pipeline_status["stage"] = "blocked"
            _pipeline_status["errors"]["profile"] = "No approved profile."
            return

        import argparse

        from .cli import _execute_run

        _pipeline_status["stage"] = "collecting"
        _pipeline_status["errors"] = {}
        _pipeline_status["stages"] = {}
        _pipeline_status["sources"] = []
        _pipeline_status["progress"] = {}
        _pipeline_status["current_source"] = None

        def on_event(event: dict[str, Any]) -> None:
            stage = str(event.get("stage") or "")
            if stage == "collecting":
                _pipeline_status["stage"] = "collecting"
            elif stage == "collected":
                _pipeline_status["stage"] = "processing"
                _pipeline_status["progress"] = {
                    "collected": event.get("collected", 0),
                    "new_jobs": event.get("new_jobs", 0),
                }
                for name, message in (event.get("source_errors") or {}).items():
                    _pipeline_status["errors"][name] = message
                _pipeline_status["counts"] = _db_counts(database)
            elif stage == "processing":
                _pipeline_status["stage"] = "processing"
                _pipeline_status["stages"] = {
                    key: value for key, value in event.items() if key != "stage"
                }
                _pipeline_status["progress"] = _pipeline_status["stages"]
            elif stage == "sources":
                _pipeline_status["sources"] = list(event.get("sources") or [])

        def on_source(event: dict[str, Any]) -> None:
            name = str(event.get("source") or "")
            phase = str(event.get("phase") or "")
            if phase == "start":
                _pipeline_status["current_source"] = name
                logger.info("dashboard: collecting source %s", name)
            elif phase == "end":
                _pipeline_status["current_source"] = None
                _pipeline_status["sources"].append(
                    {
                        "source": name,
                        "fetched": event.get("fetched"),
                        "kept": event.get("kept"),
                        "passed_filters": 0,
                        "sent": 0,
                        "error": None,
                        "elapsed": event.get("elapsed"),
                    }
                )
                logger.info(
                    "dashboard: source %s done (fetched=%s, kept=%s, %.2fs)",
                    name,
                    event.get("fetched"),
                    event.get("kept"),
                    event.get("elapsed") or 0.0,
                )
            elif phase == "error":
                _pipeline_status["current_source"] = None
                message = str(event.get("error") or "failed")
                _pipeline_status["errors"][name] = message
                _pipeline_status["sources"].append(
                    {
                        "source": name,
                        "fetched": 0,
                        "kept": 0,
                        "passed_filters": 0,
                        "sent": 0,
                        "error": message,
                        "elapsed": event.get("elapsed"),
                    }
                )
                logger.error("dashboard: source %s errored: %s", name, message)

        args = argparse.Namespace(collect=True, limit=50)
        code = _execute_run(
            args,
            run_kind="dashboard",
            on_event=on_event,
            on_source=on_source,
            database=database,
        )
        _pipeline_status["counts"] = _db_counts(database)
        progress = _pipeline_status.get("progress") or {}
        if code == 0 and (progress.get("paused") or _pipeline_status.get("stage") == "paused"):
            # The run stopped cleanly on the daily quota or the per-run token cap.
            # Keep the paused stage so /status shows the reason, not a false "done".
            _pipeline_status["stage"] = "paused"
            _pipeline_status["result"] = "paused"
        elif code == 0:
            _pipeline_status["stage"] = "done"
            _pipeline_status["result"] = "ok"
        else:
            _pipeline_status["stage"] = "error"
            _pipeline_status["errors"]["pipeline"] = f"exit code {code}"
    except Exception as exc:
        logger.exception("dashboard: pipeline worker failed")
        _pipeline_status["stage"] = "error"
        _pipeline_status["errors"]["pipeline"] = str(exc)
    finally:
        _pipeline_status["running"] = False
        _pipeline_status["current_source"] = None
        _pipeline_status["finished_at"] = time.time()


def start_pipeline(database: Path | None = None) -> bool:
    """Start one pipeline run; return ``False`` if one is already running."""
    with _pipeline_lock:
        if _pipeline_status["running"]:
            return False
        _pipeline_status.update(_blank_pipeline_status())
        _pipeline_status["running"] = True
        _pipeline_status["stage"] = "starting"
        _pipeline_status["started_at"] = time.time()
    thread = threading.Thread(target=_run_pipeline_worker, args=(database,), daemon=True)
    thread.start()
    return True


def _try_begin_single_run() -> bool:
    """Claim the one-run lock for a synchronous single-job "Add a job" evaluation.

    Uses the same lock and flag ``start_pipeline`` uses, so a pasted job and a
    full Find-jobs run can never be in flight at the same time. Returns
    ``False`` when a run (background or otherwise) is already going.
    """
    with _pipeline_lock:
        if _pipeline_status["running"]:
            return False
        _pipeline_status.update(_blank_pipeline_status())
        _pipeline_status["running"] = True
        _pipeline_status["stage"] = "processing"
        _pipeline_status["started_at"] = time.time()
    return True


def _end_single_run(result: str = "ok") -> None:
    """Release the one-run lock claimed by :func:`_try_begin_single_run`."""
    with _pipeline_lock:
        _pipeline_status["running"] = False
        _pipeline_status["stage"] = result
        _pipeline_status["result"] = result
        _pipeline_status["finished_at"] = time.time()


def _run_add_job(
    database: Path | None,
    run_id: int,
    job_id: int,
    job: Job,
    profile: CandidateProfile,
    settings: Any,
    client: LLMClient,
) -> None:
    """Evaluate one owner-added job in a background thread.

    Opens its own connection (sqlite connections are thread-affine), then owns
    the run lock and :data:`_add_state`: it closes the connection, releases the
    lock, and publishes step / rate-limit-wait progress in ``finally``.
    """
    from .pipeline import (
        LLMCallBudgetExceeded,
        LLMTokenBudgetExceeded,
        process_single_job,
    )

    def _on_step(step: str) -> None:
        with _add_lock:
            _add_state["progress"] = {
                **_add_state["progress"],
                "current_step": step,
                "llm_calls": int(getattr(client, "calls", 0) or 0),
                "tokens_used": int(getattr(client, "tokens_used", 0) or 0),
                "rate_limit_wait": None,
                "rate_limit_reason": None,
            }

    def _on_wait(seconds: float, reason: str) -> None:
        with _add_lock:
            _add_state["progress"] = {
                **_add_state["progress"],
                "rate_limit_wait": round(float(seconds), 1),
                "rate_limit_reason": reason,
                "llm_calls": int(getattr(client, "calls", 0) or 0),
                "tokens_used": int(getattr(client, "tokens_used", 0) or 0),
            }

    try:
        conn = _open(database)
    except sqlite3.Error as exc:
        logger.exception("add worker could not open the database")
        finish_run_err = str(exc)
        with _add_lock:
            _add_state["error"] = finish_run_err
        _end_single_run("failed")
        return

    try:
        result = process_single_job(
            conn,
            int(job_id),
            job,
            profile,
            settings,
            client,
            on_step=_on_step,
            on_wait=_on_wait,
        )
        finish_run(conn, run_id, "done")
        with _add_lock:
            _add_state["result_body"] = _add_outcome_html(result)
    except GroqDailyLimitReached as exc:
        finish_run(conn, run_id, "paused", {"reason": "daily"})
        with _add_lock:
            _add_state["paused"] = str(exc)
    except LLMTokenBudgetExceeded:
        finish_run(conn, run_id, "paused", {"reason": "tokens"})
        with _add_lock:
            _add_state["paused"] = (
                f"Paused: per-run token cap reached ({settings.run.max_llm_tokens_per_run} tokens)"
            )
    except LLMCallBudgetExceeded:
        finish_run(conn, run_id, "paused", {"reason": "calls"})
        with _add_lock:
            _add_state["paused"] = "Paused: per-run LLM call cap reached."
    except Exception as exc:
        logger.exception("add job %s failed", job_id)
        finish_run(conn, run_id, "failed", {"error": str(exc)})
        with _add_lock:
            _add_state["error"] = str(exc)
    finally:
        try:
            conn.close()
        except Exception:
            logger.debug("add conn close failed", exc_info=True)
        with _add_lock:
            _add_state["progress"] = {
                **_add_state["progress"],
                "llm_calls": int(getattr(client, "calls", 0) or 0),
                "tokens_used": int(getattr(client, "tokens_used", 0) or 0),
                "rate_limit_wait": None,
                "rate_limit_reason": None,
            }
            _add_state["running"] = False
            _add_state["done"] = True
            _add_state["finished_at"] = time.time()
        _end_single_run()


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #


def _load_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _recorded_text(value: Any) -> str:
    """An ISO timestamp rendered as ``YYYY-MM-DD HH:MM UTC`` (or the raw value).

    Shown next to every filter result so a stale pass/rejection is visible: the
    date is when the filter ran, which may predate the current approved profile.
    """
    if not value:
        return "-"
    text = str(value)
    if len(text) >= 16 and text[4] == "-" and text[10] in {"T", " "}:
        return text[:16].replace("T", " ") + " UTC"
    return text


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _undecided_visible(row: sqlite3.Row, max_age_days: int, now: datetime) -> bool:
    """Whether an undecided job is fresh enough for "Not yet evaluated".

    A job undecided for longer than ``max_age_days`` is stale and hidden, so the
    list reflects the current profile instead of an ever-growing backlog. The age
    is measured from ``posted_at`` when present, else from ``fetched_at``; a job
    with neither date stays visible because its age is unknown, not stale.
    """
    for key in ("posted_at", "fetched_at"):
        dt = _parse_dt(row[key])
        if dt is not None:
            return (now - dt).days <= max_age_days
    return True


def _render_check(check: dict[str, Any]) -> str:
    """One audit line as text: e.g. ``too_much_experience: min_years 1.0 <= allowed 1.0``."""
    rule_id = str(check.get("rule_id") or "-")
    if not check.get("checked"):
        return f"{rule_id}: skipped - {check.get('skipped_reason') or 'no reason'}"
    value = check.get("value")
    limit = check.get("limit")
    if value is not None and limit is not None:
        return f"{rule_id}: {value} <= {limit}"
    if value is not None:
        return f"{rule_id}: {value}"
    return rule_id


def _top_reasons(decision: dict[str, Any], limit: int = 2) -> list[str]:
    reasons: list[str] = []
    for item in decision.get("reasons_for") or []:
        if isinstance(item, dict) and item.get("quote"):
            reasons.append(str(item["quote"]))
        if len(reasons) >= limit:
            break
    return reasons


def _main_risk(decision: dict[str, Any]) -> str:
    for veto in decision.get("vetoes") or []:
        if isinstance(veto, dict) and veto.get("reason"):
            return str(veto["reason"])
    for item in decision.get("reasons_against") or []:
        if isinstance(item, dict) and item.get("quote"):
            return str(item["quote"])
    return "-"


def _verdict(evaluation: dict[str, Any]) -> dict[str, Any]:
    """The evaluator verdict nested inside a stored evaluation record."""
    verdict = evaluation.get("verdict")
    return verdict if isinstance(verdict, dict) else {}


def _seniority_text(evaluation: dict[str, Any], parsed: dict[str, Any]) -> str:
    """Human seniority from the stored verdict, else the parsed title level."""
    assessment = _verdict(evaluation).get("seniority_assessment")
    if isinstance(assessment, dict) and assessment.get("quote"):
        return str(assessment["quote"])
    level = parsed.get("title_seniority")
    if level and level != Seniority.unknown.value:
        return f"title: {level}"
    return Seniority.unknown.value


def _seniority_unknown(evaluation: dict[str, Any], parsed: dict[str, Any]) -> bool:
    return _seniority_text(evaluation, parsed) == Seniority.unknown.value


def _outcome_and_warning(
    decision: dict[str, Any], evaluation: dict[str, Any], parsed: dict[str, Any]
) -> tuple[str, str]:
    """Display outcome plus an informational seniority warning.

    The stored outcome is shown verbatim: the decision engine itself sends
    unknown/mid seniority to REVIEW, so this no longer downgrades APPLY. The
    warning is presentation only and never changes filters, ranking or outcome.
    """
    outcome = str(decision.get("outcome") or "").lower()
    if _seniority_unknown(evaluation, parsed):
        return outcome, '<div class="warn">seniority unknown</div>'
    return outcome, ""


def _row_html(row: sqlite3.Row) -> str:
    job_id = int(row["id"])
    url = str(row["url"] or "")
    decision = _load_json(row["decision_json"])
    evaluation = _load_json(row["evaluation_json"])
    parsed = _load_json(row["parsed_json"])

    title = html.escape(str(row["title"] or url or f"job {job_id}"))
    job_cell = (
        f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">{title}</a>'
        if is_http_url(url)
        else title
    )
    outcome, warning = _outcome_and_warning(decision, evaluation, parsed)
    score = decision.get("match_score")
    score_cell = f"{score:.0f}" if isinstance(score, (int, float)) else "-"
    reasons = " | ".join(html.escape(r) for r in _top_reasons(decision)) or "-"
    missing = ", ".join(html.escape(str(s)) for s in (decision.get("missing_skills") or [])) or "-"

    current = str(row["application_status"] or "PEND")
    if current not in _LABELS:
        current = "PEND"
    actions = "".join(
        (
            f'<form method="post" action="/jobs/{job_id}/status/{code}">'
            f'<button class="badge" type="submit" style="background:{_COLORS[code]}">'
            f"{_LABELS[code]}</button></form>"
        )
        for code in APPLICATION_STATUSES
    )
    prepare = (
        f'<a class="badge" style="background:#8250df" href="/jobs/{job_id}/kit">'
        "Prepare application</a> "
    )
    return (
        f'<tr><td><a href="/jobs/{job_id}">{job_id}</a></td><td>{job_cell}</td>'
        f"<td>{html.escape(str(row['company'] or '-'))}</td>"
        f"<td>{html.escape(str(row['location'] or '-'))}</td>"
        f"<td>{html.escape(str(row['source'] or '-'))}</td>"
        f"<td>{html.escape(outcome.upper() or '-')} {warning}</td>"
        f"<td>{html.escape(score_cell)}</td>"
        f"<td>{reasons}</td>"
        f"<td>{html.escape(_main_risk(decision))}</td>"
        f"<td>{html.escape(missing)}</td>"
        f"<td>{html.escape(_seniority_text(evaluation, parsed))}</td>"
        f'<td><span class="badge" style="background:{_COLORS[current]}">'
        f"{_LABELS[current]}</span></td>"
        f'<td class="actions">{prepare}{actions}</td></tr>\n'
    )


_TABLE_HEAD = """<table>
<thead><tr><th>#</th><th>Job</th><th>Company</th><th>Location</th>
<th>Source</th><th>Decision</th><th>Score</th><th>Reasons for</th>
<th>Main risk</th><th>Missing skills</th><th>Seniority</th>
<th>Apply status</th><th>Set</th></tr></thead>
<tbody>
"""

_UNDECIDED_HEAD = """<table>
<thead><tr><th>#</th><th>Job</th><th>Company</th><th>Location</th>
<th>Source</th><th>Seniority</th><th>Filtered</th><th>Why</th></tr></thead>
<tbody>
"""


def _undecided_row_html(row: sqlite3.Row) -> str:
    job_id = int(row["id"])
    url = str(row["url"] or "")
    evaluation = _load_json(row["evaluation_json"])
    parsed = _load_json(row["parsed_json"])
    title = html.escape(str(row["title"] or url or f"job {job_id}"))
    job_cell = (
        f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">{title}</a>'
        if is_http_url(url)
        else title
    )
    return (
        f'<tr><td><a href="/jobs/{job_id}">{job_id}</a></td><td>{job_cell}</td>'
        f"<td>{html.escape(str(row['company'] or '-'))}</td>"
        f"<td>{html.escape(str(row['location'] or '-'))}</td>"
        f"<td>{html.escape(str(row['source'] or '-'))}</td>"
        f"<td>{html.escape(_seniority_text(evaluation, parsed))}</td>"
        f"<td>{html.escape(_recorded_text(row['filter_created_at']))}</td>"
        "<td>passed all filters, no decision stored yet</td></tr>\n"
    )


def _source_label(source: EvidenceSource) -> str:
    """Display name for an evidence source: ``profile`` or ``job``."""
    return "profile" if source.value == "profile" else "job"


def _copy_button(target_id: str, label: str = "Copy") -> str:
    """Client-side copy button; reads the target element's rendered text."""
    return (
        f'<button type="button" class="badge" '
        f"onclick=\"navigator.clipboard.writeText(document.getElementById('{target_id}')"
        f'.textContent)">{html.escape(label)}</button>'
    )


def _kit_claim_rows(claims: list[Any]) -> str:
    return "".join(
        f"<tr><td>{html.escape(claim.text)}</td>"
        f"<td>&ldquo;{html.escape(claim.quote)}&rdquo;</td>"
        f"<td>{html.escape(_source_label(claim.source))}</td></tr>"
        for claim in claims
    )


def _kit_lines_html(section: Any, anchor: str) -> str:
    """One summary/cover table: each sentence next to its quote and source."""
    if not section.claims:
        return f'<p class="error">{html.escape(section.blocked_reason or "needs your input")}</p>'
    table = (
        "<table>"
        "<thead><tr><th>Sentence</th><th>Supporting quote</th><th>Source</th></tr></thead>"
        f"<tbody>{_kit_claim_rows(section.claims)}</tbody></table>"
    )
    if section.blocked_reason:
        table += f'<p class="error">{html.escape(section.blocked_reason)}</p>'
    return f'<div id="{anchor}">{table}</div>' + _copy_button(anchor)


def _kit_answers_html(kit: ApplicationKit) -> str:
    if not kit.answers:
        return '<p class="error">needs your input</p>'
    rows = "".join(
        f"<tr><td>{html.escape(answer.question)}<br>{html.escape(answer.answer)}</td>"
        f"<td>&ldquo;{html.escape(answer.quote)}&rdquo;</td>"
        f"<td>{html.escape(_source_label(answer.source))}</td></tr>"
        for answer in kit.answers
    )
    return (
        '<div id="kit-answers"><table>'
        "<thead><tr><th>Question &amp; answer</th><th>Supporting quote</th>"
        "<th>Source</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    ) + _copy_button("kit-answers")


def _kit_body_html(kit: ApplicationKit) -> str:
    """Render a stored kit: summary, cover note and answers, each claim shown
    next to its supporting quote and source. Nothing here is applied for the
    user: the page only displays and copies text."""
    parts = ["<h2>Summary</h2>\n", _kit_lines_html(kit.summary, "kit-summary"), "\n"]
    parts.append("<h2>Cover note</h2>\n")
    parts.append(_kit_lines_html(kit.cover_note, "kit-cover"))
    parts.append("\n<h2>Answers</h2>\n")
    parts.append(_kit_answers_html(kit))
    parts.append("\n")
    if kit.prompt_version:
        parts.append(
            f'<p class="notice">prompt v{html.escape(kit.prompt_version)} '
            f"· model {html.escape(kit.model or '-')}</p>\n"
        )
    return "".join(parts)


def _kit_page(
    row: sqlite3.Row,
    kit: ApplicationKit | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """Application-kit page for one job.

    Shows the posting, the apply link, a "mark as APLD" button, and — once
    generated — the kit with every claim next to its supporting quote and
    source. It never submits an application.
    """
    job_id = int(row["id"])
    url = str(row["url"] or "")
    title = html.escape(str(row["title"] or url or f"job {job_id}"))
    company = html.escape(str(row["company"] or "-"))
    location = html.escape(str(row["location"] or "-"))
    source = html.escape(str(row["source"] or "-"))
    parts = [_HEAD, _nav(), "<h1>Application kit</h1>\n"]
    if notice:
        parts.append(f'<p class="notice">{html.escape(notice)}</p>\n')
    if error:
        parts.append(f'<p class="error">{html.escape(error)}</p>\n')
    if is_http_url(url):
        apply_link = (
            f'<a class="badge" style="background:#0969da" '
            f'href="{html.escape(url, quote=True)}" rel="noreferrer" '
            f'target="_blank">Apply link (opens posting)</a>'
        )
    else:
        apply_link = html.escape(url or "-")
    parts.append(
        f"<p>{title}<br>Company: {company}<br>Location: {location}<br>"
        f"Source: {source}<br>Apply: {apply_link}</p>\n"
    )
    parts.append(
        f'<p><form method="post" action="/jobs/{job_id}/status/APLD">'
        '<button type="submit" class="badge" style="background:#1a7f37">'
        "Mark as APLD (applied)</button></form></p>\n"
    )
    if kit is not None:
        parts.append(_kit_body_html(kit))
    else:
        parts.append("<p>No kit generated yet.</p>\n")
    action = "Regenerate" if kit is not None else "Generate"
    parts.append(
        f'<form method="post" action="/jobs/{job_id}/kit">'
        f'<button type="submit">{action} application kit</button></form>\n'
    )
    parts.append('<p><a href="/">Back to results</a></p>\n')
    parts.append(_TABLE_FOOT)
    return "".join(parts)


def _kit_error_page(message: str) -> str:
    return (
        _HEAD
        + _nav()
        + "<h1>Application kit</h1>"
        + f'<p class="error">{html.escape(message)}</p>'
        + _TABLE_FOOT
    )


def _page(
    decided: list[sqlite3.Row],
    undecided: list[sqlite3.Row],
    notice: str | None = None,
) -> str:
    parts = [_HEAD, _nav(), "<h1>Atlas — matched jobs</h1>\n"]
    if notice:
        parts.append(f'<p class="notice">{html.escape(notice)}</p>\n')
    parts.append(
        '<p><form method="post" action="/find">'
        '<button type="submit">Find jobs</button></form></p>\n'
    )
    parts.append(
        f"<p>{len(decided)} matched job(s) with an APPLY/REVIEW decision. "
        "Apply status is manual bookkeeping.</p>\n"
    )
    parts.append(_TABLE_HEAD)
    if decided:
        parts.extend(_row_html(row) for row in decided)
    else:
        parts.append(
            '<tr><td colspan="13">No APPLY/REVIEW jobs yet. '
            "Click <strong>Find jobs</strong> to run the pipeline.</td></tr>\n"
        )
    parts.append("</table>\n")
    parts.append(f"<h2>Not yet evaluated ({len(undecided)})</h2>\n")
    if undecided:
        parts.append(_UNDECIDED_HEAD)
        parts.extend(_undecided_row_html(row) for row in undecided)
        parts.append("</table>\n")
    else:
        parts.append("<p>None — every job that passed the filters has a decision.</p>\n")
    parts.append(_TABLE_FOOT)
    return "".join(parts)


def _open(database: Path | None) -> sqlite3.Connection:
    conn = connect(database)
    init_db(conn)
    return conn


def _add_form(
    error: str | None = None,
    notice: str | None = None,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
    apply_url: str = "",
    description: str = "",
) -> str:
    """The "Add a job" form. Values are re-filled and HTML-escaped on error."""
    parts = [_HEAD, _nav(), "<h1>Add a job</h1>\n"]
    parts.append(
        "<p>Paste a job you found yourself. The description is evaluated through "
        "the same pipeline as collected jobs; the apply link is stored only and "
        "<strong>never fetched</strong>.</p>\n"
    )
    if error:
        parts.append(f'<p class="error">{html.escape(error)}</p>\n')
    if notice:
        parts.append(f'<p class="notice">{html.escape(notice)}</p>\n')
    parts.append(
        '<form method="post" action="/add">'
        f'<p><label>Title:<br><input type="text" name="title" '
        f'value="{html.escape(title, quote=True)}"></label></p>'
        f'<p><label>Company:<br><input type="text" name="company" '
        f'value="{html.escape(company, quote=True)}"></label></p>'
        f'<p><label>Location:<br><input type="text" name="location" '
        f'value="{html.escape(location, quote=True)}"></label></p>'
        f'<p><label>Apply URL (optional, http/https):<br><input type="text" name="apply_url" '
        f'value="{html.escape(apply_url, quote=True)}"></label></p>'
        f"<p><label>Job description (required, max {MAX_JD_CHARS:,} characters):<br>"
        f'<textarea name="description" rows="14">{html.escape(description)}</textarea></label></p>'
        '<button type="submit">Evaluate job</button></form>'
    )
    parts.append('<p><a href="/">Back to results</a></p>\n')
    parts.append(_TABLE_FOOT)
    return "".join(parts)


def _evidence_items(items: list[Any]) -> str:
    """Render a list of Evidence objects (quote + source) as <li> rows."""
    if not items:
        return "<li>-</li>\n"
    out = []
    for item in items:
        quote = html.escape(str(getattr(item, "quote", "") or ""))
        source = getattr(item, "source", "")
        source = getattr(source, "value", source)
        out.append(f"<li>&ldquo;{quote}&rdquo; <em>({html.escape(str(source))})</em></li>\n")
    return "".join(out)


def _add_outcome_html(result: Any) -> str:
    """Render the decision, score and reasons (or the rejection) for one job."""
    filter_result = getattr(result, "filter_result", None)
    if filter_result is not None and not filter_result.passed:
        rejections = getattr(filter_result, "rejections", []) or []
        parts = [
            "<h2>Rejected</h2>\n",
            '<p class="error">Rejected by a hard filter. No LLM was called for this job.</p>\n',
            "<ul>\n",
        ]
        for rejection in rejections:
            parts.append(
                f"<li>rule <strong>{html.escape(str(rejection.rule_id))}</strong>: "
                f"{html.escape(str(rejection.evidence))}</li>\n"
            )
        parts.append("</ul>\n")
        return "".join(parts)
    decision = getattr(result, "decision", None)
    if decision is None:
        return (
            "<h2>No decision</h2>\n"
            '<p class="warn">The job was saved but no decision was stored '
            "(it needs a human review or the run paused).</p>\n"
        )
    outcome = getattr(decision.outcome, "value", decision.outcome)
    score = getattr(decision, "match_score", 0.0)
    parts = [
        f"<h2>Decision: {html.escape(str(outcome).upper())}</h2>\n",
        f"<p>Match score: <strong>{html.escape(str(score))}</strong></p>\n",
        "<h3>Reasons for</h3>\n<ul>\n",
        _evidence_items(getattr(decision, "reasons_for", [])),
        "</ul>\n",
        "<h3>Reasons against</h3>\n<ul>\n",
        _evidence_items(getattr(decision, "reasons_against", [])),
        "</ul>\n",
    ]
    return "".join(parts)


def _score_line(decision: dict[str, Any]) -> str:
    """The headline score with the REVIEW threshold, e.g. ``55 / 100 (REVIEW starts at 65)``."""
    review = get_settings().decision.review_threshold
    score = decision.get("match_score")
    if isinstance(score, (int, float)):
        return f"{score:.0f} / 100 (REVIEW starts at {review:.0f})"
    return f"- / 100 (REVIEW starts at {review:.0f})"


def _confidence_text(decision: dict[str, Any]) -> str:
    value = decision.get("confidence")
    return f"{value:.0%}" if isinstance(value, (int, float)) else "-"


def _dimension_table(decision: dict[str, Any]) -> str:
    """Score breakdown by dimension: value, weight and disagreement spread."""
    dimensions = decision.get("dimensions") or {}
    if not dimensions:
        return "<p>No dimension breakdown was stored for this decision.</p>\n"
    weights = decision.get("weights") or {}
    spreads = decision.get("spreads") or {}
    rows = []
    for name in sorted(dimensions):
        score = dimensions.get(name)
        rows.append(
            f"<tr><td>{html.escape(str(name))}</td>"
            f"<td>{score:.1f}</td>"
            f"<td>{weights.get(name, 0.0):.2f}</td>"
            f"<td>{spreads.get(name, 0.0):.1f}</td></tr>\n"
        )
    return (
        "<table><thead><tr><th>Dimension</th><th>Score</th><th>Weight</th>"
        f"<th>Spread</th></tr></thead><tbody>{''.join(rows)}</tbody></table>\n"
    )


def _vetoes_html(decision: dict[str, Any]) -> str:
    """Each veto as a one-line rule + reason, plus its quote and source."""
    vetoes = decision.get("vetoes") or []
    if not vetoes:
        return "<p>No vetoes.</p>\n"
    out = []
    for veto in vetoes:
        rule = html.escape(str(veto.get("rule_id") or "-"))
        kind = "hard" if veto.get("hard", True) else "soft"
        reason = html.escape(str(veto.get("reason") or ""))
        evidence = veto.get("evidence") or {}
        quote = html.escape(str((evidence or {}).get("quote") or ""))
        source = html.escape(str((evidence or {}).get("source") or ""))
        line = f"<li><strong>{rule}</strong> ({kind}): {reason}"
        if quote:
            line += f"<br>&ldquo;{quote}&rdquo; <em>({source})</em>"
        out.append(line + "</li>\n")
    return "<ul>\n" + "".join(out) + "</ul>\n"


def _evidence_quote(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("quote") or "").strip()
    return str(getattr(item, "quote", "") or "").strip()


def _evidence_source(item: Any) -> str:
    source = item.get("source") if isinstance(item, dict) else getattr(item, "source", "")
    return str(getattr(source, "value", source) or "")


def _reason_explanation(item: Any) -> str:
    """A reason's optional one-line explanation; plain evidence has none."""
    if not isinstance(item, dict):
        return ""
    for key in ("explanation", "reason", "why", "rationale"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _dedupe_reasons(
    decision: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Dedupe reasons by quote across both sides.

    Returns ``(cited_on_both_sides, only_for, only_against)``; each quote appears
    at most once. An explanation is taken from whichever side carried one.
    """
    for_items = [i for i in (decision.get("reasons_for") or []) if _evidence_quote(i)]
    against_items = [i for i in (decision.get("reasons_against") or []) if _evidence_quote(i)]
    for_quotes = {_evidence_quote(i) for i in for_items}
    against_quotes = {_evidence_quote(i) for i in against_items}
    against_explanations = {_evidence_quote(i): _reason_explanation(i) for i in against_items}

    def entries(items: list[Any], predicate: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            quote = _evidence_quote(item)
            if quote in seen or not predicate(quote):
                continue
            seen.add(quote)
            explanation = _reason_explanation(item) or against_explanations.get(quote, "")
            out.append(
                {"quote": quote, "source": _evidence_source(item), "explanation": explanation}
            )
        return out

    both = entries(for_items, lambda q: q in against_quotes)
    only_for = entries(for_items, lambda q: q not in against_quotes)
    only_against = entries(against_items, lambda q: q not in for_quotes)
    return both, only_for, only_against


def _reason_list_html(items: list[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        lead = (
            f"<strong>{html.escape(item['explanation'])}</strong><br>"
            if item.get("explanation")
            else ""
        )
        rows.append(
            f"<li>{lead}&ldquo;{html.escape(item['quote'])}&rdquo; "
            f"<em>({html.escape(item['source'])})</em></li>\n"
        )
    return "<ul>\n" + "".join(rows) + "</ul>\n"


def _reasons_html(decision: dict[str, Any]) -> str:
    """Reasons and evidence, each quote once.

    An explained reason is shown as ``explanation`` + quote + source. A reason
    with no explanation is listed under "Evidence" (quote + source only). A quote
    cited on both sides appears once, under "Cited on both sides".
    """
    both, only_for, only_against = _dedupe_reasons(decision)
    evidenced = [i for i in only_for if not i["explanation"]]
    evidenced += [i for i in only_against if not i["explanation"]]
    parts: list[str] = []
    for title, items in (
        ("Reasons for", [i for i in only_for if i["explanation"]]),
        ("Reasons against", [i for i in only_against if i["explanation"]]),
        ("Cited on both sides", both),
        ("Evidence", evidenced),
    ):
        if items:
            parts.append(f"<h3>{title}</h3>\n{_reason_list_html(items)}")
    if not parts:
        return "<p>No reasons or evidence were stored for this decision.</p>\n"
    return "".join(parts)


def _owner_verdict_html(verdict: dict[str, Any] | None) -> str:
    if verdict is None:
        return "<p>You have not recorded a verdict for this job yet.</p>\n"
    label = html.escape(str(verdict.get("verdict") or "").upper())
    reason = html.escape(str(verdict.get("reason") or "-"))
    system = html.escape(str(verdict.get("system_decision") or "-"))
    when = html.escape(_recorded_text(verdict.get("created_at")))
    return (
        f"<p>Your verdict: <strong>{label}</strong> &mdash; {reason}<br>"
        f'<span class="notice">system decision at the time: {system} &middot; {when}</span></p>\n'
    )


def _owner_verdict_form(job_id: int) -> str:
    options = "".join(
        f'<option value="{code}">{code.upper()}</option>' for code in OWNER_VERDICT_CHOICES
    )
    return (
        f'<form method="post" action="/jobs/{job_id}/verdict">'
        f'<label>Verdict: <select name="verdict">{options}</select></label> '
        '<label>Reason: <input type="text" name="reason" maxlength="200" '
        'placeholder="one line"></label> '
        '<button type="submit">Your verdict</button></form>\n'
    )


def _job_error_page(message: str) -> str:
    return (
        _HEAD
        + _nav()
        + "<h1>Job result</h1>"
        + f'<p class="error">{html.escape(message)}</p>'
        + '<p><a href="/">Back to results</a></p>'
        + _TABLE_FOOT
    )


def _job_result_page(
    row: sqlite3.Row,
    *,
    owner_verdict: dict[str, Any] | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> str:
    """One job's full, auditable result: score, dimensions, vetoes and reasons."""
    job_id = int(row["id"])
    url = str(row["url"] or "")
    decision = _load_json(row["decision_json"])
    evaluation = _load_json(row["evaluation_json"])
    parsed = _load_json(row["parsed_json"])
    title = html.escape(str(row["title"] or url or f"job {job_id}"))
    job_cell = (
        f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">{title}</a>'
        if is_http_url(url)
        else title
    )
    parts = [_HEAD, _nav(), f"<h1>Result &mdash; {title}</h1>\n"]
    if notice:
        parts.append(f'<p class="notice">{html.escape(notice)}</p>\n')
    if error:
        parts.append(f'<p class="error">{html.escape(error)}</p>\n')
    parts.append(
        f"<p>Company: {html.escape(str(row['company'] or '-'))}<br>"
        f"Location: {html.escape(str(row['location'] or '-'))}<br>"
        f"Source: {html.escape(str(row['source'] or '-'))}<br>"
        f"Job: {job_cell}</p>\n"
    )
    if not decision:
        parts.append('<p class="warn">No decision is stored for this job yet.</p>\n')
    else:
        outcome = str(decision.get("outcome") or "-").lower()
        missing = ", ".join(str(s) for s in (decision.get("missing_skills") or [])) or "-"
        parts.append(f"<h2>Decision: {html.escape(outcome.upper())}</h2>\n")
        parts.append(f"<p>Score: <strong>{html.escape(_score_line(decision))}</strong></p>\n")
        parts.append(
            f"<p>Confidence: <strong>{html.escape(_confidence_text(decision))}</strong></p>\n"
        )
        parts.append(
            f"<p>Seniority assessment: {html.escape(_seniority_text(evaluation, parsed))}</p>\n"
        )
        parts.append(f"<p>Missing skills: {html.escape(missing)}</p>\n")
        parts.append("<h3>Score breakdown</h3>\n")
        parts.append(_dimension_table(decision))
        parts.append("<h3>Vetoes</h3>\n")
        parts.append(_vetoes_html(decision))
        parts.append(_reasons_html(decision))
    parts.append("<h2>Prepare application</h2>\n")
    parts.append(
        f'<p><a class="badge" style="background:#8250df" href="/jobs/{job_id}/kit">'
        "Prepare application</a></p>\n"
    )
    parts.append("<h2>Your verdict</h2>\n")
    parts.append(_owner_verdict_html(owner_verdict))
    parts.append(_owner_verdict_form(job_id))
    parts.append('<p><a href="/">Back to results</a></p>\n')
    parts.append(_TABLE_FOOT)
    return "".join(parts)


def _add_result_page(
    job_id: int | None = None,
    *,
    body: str = "",
    duplicate: bool = False,
    paused: str | None = None,
) -> str:
    parts = [_HEAD, _nav(), "<h1>Add a job</h1>\n"]
    if duplicate:
        parts.append(
            '<p class="notice">This job is already in your list (it matched an '
            "existing entry by company, title and location).</p>\n"
        )
        if job_id is not None:
            parts.append(
                f'<p><a href="/jobs/{job_id}/kit">Open the existing job</a> &middot; '
                '<a href="/">Back to results</a></p>\n'
            )
        else:
            parts.append('<p><a href="/">Back to results</a></p>\n')
    elif paused is not None:
        parts.append(f'<p class="warn">{html.escape(paused)}</p>\n')
        parts.append("<p>The job was saved and left undecided; the next run will pick it up.</p>\n")
        parts.append('<p><a href="/">Back to results</a></p>\n')
    else:
        parts.append(body)
        parts.append('<p><a href="/">Back to results</a></p>\n')
    parts.append(_TABLE_FOOT)
    return "".join(parts)


def _add_result_page_view() -> str:
    """Render the final ``/add`` result from the current add state."""
    with _add_lock:
        state = dict(_add_state)
    if state.get("error"):
        return _add_result_page(
            body=f'<p class="error">{html.escape(str(state["error"]))}</p>\n'
            "<p>The job was saved and left undecided; the next run will pick it up.</p>\n"
        )
    if state.get("paused") is not None:
        return _add_result_page(paused=str(state["paused"]))
    if state.get("result_body"):
        return _add_result_page(body=str(state["result_body"]))
    return _add_result_page(
        body='<p class="warn">This job is still being evaluated.</p>\n',
    )


def _add_progress_page() -> str:
    """Polling page shown right after ``POST /add`` hands off to the worker.

    Renders the current add state server-side (so a reload shows the latest
    step / rate-limit wait / counters immediately) and keeps polling
    ``/api/add-status`` to update them live until the worker is done.
    """
    with _add_lock:
        state = dict(_add_state)
    progress = state.get("progress") or {}
    step = html.escape(str(progress.get("current_step") or "Starting\u2026"))
    calls = html.escape(str(progress.get("llm_calls") or 0))
    tokens = html.escape(str(progress.get("tokens_used") or 0))
    wait_seconds = progress.get("rate_limit_wait")
    if wait_seconds:
        reason = html.escape(str(progress.get("rate_limit_reason") or "rate limit"))
        wait = (
            f'<p id="add-wait" class="warn">Waiting for Groq {reason}, '
            f"retry in {html.escape(str(wait_seconds))}s\u2026</p>\n"
        )
    else:
        wait = '<p id="add-wait" class="warn" hidden></p>\n'
    body = (
        "<h1>Evaluating your job</h1>\n"
        f'<p id="add-step" data-state="running">{step}</p>\n'
        f"{wait}"
        f'<p>LLM calls: <strong id="add-calls">{calls}</strong> &middot; '
        f'Tokens used: <strong id="add-tokens">{tokens}</strong></p>\n'
        '<p><a href="/add">Add another job</a> &middot; '
        '<a href="/">Back to results</a></p>\n'
        "<script>\n"
        "(async function () {\n"
        "  const step = document.getElementById('add-step');\n"
        "  const wait = document.getElementById('add-wait');\n"
        "  const calls = document.getElementById('add-calls');\n"
        "  const tokens = document.getElementById('add-tokens');\n"
        "  while (true) {\n"
        "    let data = null;\n"
        "    try {\n"
        "      const res = await fetch('/api/add-status', {credentials: 'same-origin'});\n"
        "      if (res.ok) data = await res.json();\n"
        "    } catch (e) { /* transient poll error: retry below */ }\n"
        "    if (data) {\n"
        "      if (data.current_step) step.textContent = data.current_step;\n"
        "      calls.textContent = String(data.llm_calls ?? 0);\n"
        "      tokens.textContent = String(data.tokens_used ?? 0);\n"
        "      if (data.rate_limit_wait) {\n"
        "        wait.hidden = false;\n"
        "        const reason = data.rate_limit_reason || 'rate limit';\n"
        "        wait.textContent = 'Waiting for Groq ' + reason + ', retry in ' +\n"
        "          data.rate_limit_wait + 's\\u2026';\n"
        "      } else { wait.hidden = true; }\n"
        "      if (data.done) {\n"
        "        step.textContent = 'Finished';\n"
        "        const res = await fetch('/add/progress', {credentials: 'same-origin'});\n"
        "        document.open();\n"
        "        document.write(await res.text());\n"
        "        document.close();\n"
        "        return;\n"
        "      }\n"
        "    }\n"
        "    await new Promise(r => setTimeout(r, 1000));\n"
        "  }\n"
        "}());\n"
        "</script>\n"
    )
    return _add_result_page(body=body)


def _recover_runs(database: Path | None) -> None:
    """On startup, fail abandoned runs and restore the last run's status.

    A run whose process was killed leaves ``runs.status='running'`` forever, and
    the Status page keeps advertising a run that is not happening. Anything
    still ``running`` past the stale threshold is marked ``failed`` here, then
    the latest run's persisted summary is loaded so ``/status`` shows the last
    known per-stage/per-source progress instead of a blank idle screen.
    """
    if _pipeline_status["running"] or _pipeline_status["stage"] != "idle":
        return
    try:
        conn = _open(database)
    except sqlite3.Error:
        return
    try:
        failed = fail_stale_runs(conn)
        row = latest_run(conn)
        summary = _load_json(row["summary_json"]) if row is not None else {}
        errors = dict(summary.get("errors") or {})
        if summary.get("error"):
            errors["history"] = str(summary["error"])
        if failed:
            errors["startup"] = "marked stale run(s) failed on startup: " + ", ".join(
                str(run_id) for run_id in failed
            )
    except sqlite3.Error:
        return
    finally:
        conn.close()
    if row is None:
        return
    if "stage" not in summary and row["status"] != "failed":
        # Not a pipeline run we can rehydrate (e.g. a one-off eval or collect).
        return
    stage = "failed" if row["status"] == "failed" else str(summary.get("stage") or row["status"])
    _pipeline_status["stage"] = stage
    _pipeline_status["stages"] = summary.get("stages") or {}
    _pipeline_status["progress"] = summary.get("stages") or {}
    _pipeline_status["sources"] = summary.get("sources") or []
    _pipeline_status["counts"] = summary.get("counts") or {}
    _pipeline_status["errors"] = errors
    _pipeline_status["result"] = summary.get("result")
    _pipeline_status["recovered_run_id"] = int(row["id"])


def _same_origin(request: Request) -> bool:
    """CSRF guard: reject POSTs whose Origin/Referer points at another site."""
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if candidate is None:
        return True
    return urlparse(candidate).netloc == request.url.netloc


def _parse_skills(text: str) -> list[Skill]:
    skills: list[Skill] = []
    for chunk in text.replace("\n", ",").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        parts = entry.split(":", 3)
        name = parts[0].strip()
        if not name:
            continue
        prof = parts[1].strip() if len(parts) > 1 else ""
        evidenced_by = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        confirm_raw = parts[3].strip().lower() if len(parts) > 3 else ""
        confirmed = confirm_raw in {"confirm", "confirmed", "yes", "true", "1"}
        try:
            proficiency = Proficiency(prof.lower())
        except ValueError:
            proficiency = Proficiency.familiar
        skills.append(
            Skill(
                name=name,
                canonical_name=normalize_skill_name(name),
                proficiency=proficiency,
                evidenced_by=evidenced_by,
                confirmed=confirmed,
            )
        )
    return skills


def _form_list(form: FormData, key: str) -> list[str]:
    """All values for ``key`` in submission order (missing key -> empty list)."""
    return [str(value) for value in form.getlist(key)]


def _at(values: list[str], index: int) -> str:
    return values[index] if index < len(values) else ""


def _split_items(text: str) -> list[str]:
    """Split on commas and newlines, stripping blanks (matches old comma split)."""
    return [
        part.strip()
        for part in text.replace("\r", "\n").replace("\n", ",").split(",")
        if part.strip()
    ]


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "\n").split("\n") if line.strip()]


def _int_or_none(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _truthy(text: str) -> bool:
    return text.strip().lower() in {"1", "true", "yes", "on"}


def _parse_experiences(form: FormData, prefix: str) -> list[Experience]:
    """Rebuild internship/work entries from parallel repeated form fields.

    Every rendered row submits all fields (blank when empty) so the lists stay
    index-aligned; a row whose every field is blank is dropped, never invented.
    """
    roles = _form_list(form, f"{prefix}role")
    companies = _form_list(form, f"{prefix}company")
    months = _form_list(form, f"{prefix}months")
    dates = _form_list(form, f"{prefix}dates")
    tech = _form_list(form, f"{prefix}tech")
    descriptions = _form_list(form, f"{prefix}description")
    count = max(len(roles), len(companies), len(months), len(dates), len(tech), len(descriptions))
    entries: list[Experience] = []
    for index in range(count):
        role = _at(roles, index).strip()
        company = _at(companies, index).strip()
        month = _at(months, index)
        date = _at(dates, index).strip()
        techs = _split_items(_at(tech, index))
        description = _split_lines(_at(descriptions, index))
        if not any((role, company, month.strip(), date, techs, description)):
            continue
        entries.append(
            Experience(
                role=role or None,
                company=company or None,
                duration_months=_int_or_none(month),
                dates=date or None,
                tech=techs,
                description=description,
            )
        )
    return entries


def _parse_projects(form: FormData) -> list[Project]:
    titles = _form_list(form, "project_title")
    summaries = _form_list(form, "project_summary")
    outcomes = _form_list(form, "project_outcomes")
    tech = _form_list(form, "project_tech")
    count = max(len(titles), len(summaries), len(outcomes), len(tech))
    entries: list[Project] = []
    for index in range(count):
        title = _at(titles, index).strip()
        summary = _at(summaries, index).strip()
        outcome = _at(outcomes, index).strip()
        techs = _split_items(_at(tech, index))
        if not any((title, summary, outcome, techs)):
            continue
        entries.append(
            Project(
                title=title or None,
                summary=summary or None,
                outcomes=outcome or None,
                tech=techs,
            )
        )
    return entries


def _parse_education(form: FormData) -> list[Education]:
    degrees = _form_list(form, "education_degree")
    fields = _form_list(form, "education_field")
    institutions = _form_list(form, "education_institution")
    years = _form_list(form, "education_year")
    expected = _form_list(form, "education_expected")
    count = max(len(degrees), len(fields), len(institutions), len(years), len(expected))
    entries: list[Education] = []
    for index in range(count):
        degree = _at(degrees, index).strip()
        field = _at(fields, index).strip()
        institution = _at(institutions, index).strip()
        year = _at(years, index)
        flag = _truthy(_at(expected, index))
        if not any((degree, field, institution, year.strip(), flag)):
            continue
        entries.append(
            Education(
                degree=degree or None,
                field=field or None,
                institution=institution or None,
                graduation_year=_int_or_none(year),
                expected=flag,
            )
        )
    return entries


def _parse_links(form: FormData) -> tuple[Links, list[str]]:
    """Return (links, rejected_labels). Only absolute http/https URLs are kept."""
    links = Links()
    rejected: list[str] = []
    for label in ("github", "linkedin", "portfolio"):
        raw = str(form.get(label, "")).strip()
        if not raw:
            continue
        if not is_http_url(raw):
            rejected.append(label)
            continue
        setattr(links, label, raw)
    return links, rejected


def _entry_name(experience: Experience) -> str:
    role = (experience.role or "").strip() or "(role not set)"
    company = (experience.company or "").strip() or "(company not set)"
    return f"{role} @ {company}"


def _entries_missing_text(
    internships: list[Experience], work: list[Experience], projects: list[Project]
) -> list[str]:
    """Human labels for entries that lack the description/outcome text."""
    missing: list[str] = []
    for label, entries in (("internship", internships), ("work", work)):
        for entry in entries:
            if not entry.description:
                missing.append(f"{label}: {_entry_name(entry)}")
    for project in projects:
        if not (project.summary or project.outcomes):
            missing.append(f"project: {(project.title or '').strip() or '(untitled)'}")
    return missing


_SECTION_SCRIPT = """<script>
function atlasNewRow(templateId, listId) {
  var tpl = document.getElementById(templateId);
  var list = document.getElementById(listId);
  var node = tpl.content.firstElementChild.cloneNode(true);
  list.appendChild(node);
}
document.addEventListener('click', function (event) {
  var target = event.target;
  if (target.dataset && target.dataset.removeRow !== undefined) {
    event.preventDefault();
    var row = target.closest('[data-row]');
    if (row) { row.parentNode.removeChild(row); }
  } else if (target.dataset && target.dataset.addRows) {
    event.preventDefault();
    atlasNewRow(target.dataset.addRows, target.dataset.list);
  }
});
</script>"""


def _skill_text(profile: CandidateProfile) -> str:
    lines: list[str] = []
    for s in profile.skills:
        parts = [s.name, str(getattr(s.proficiency, "value", s.proficiency)), s.evidenced_by or ""]
        if s.confirmed:
            parts.append("confirm")
        lines.append(":".join(parts))
    return "\n".join(lines)


def _experience_row(entry: Experience | None) -> str:
    entry = entry or Experience()
    tech = ", ".join(entry.tech)
    description = "\n".join(entry.description)
    return (
        "<fieldset data-row>"
        "<label>Role<br>"
        f'<input type="text" name="__PREFIX__role" value="{html.escape(entry.role or "")}"></label>'
        "<label>Company<br>"
        f'<input type="text" name="__PREFIX__company" value="{html.escape(entry.company or "")}"></label>'
        "<label>Months (number)<br>"
        f'<input type="text" name="__PREFIX__months" value="{entry.duration_months if entry.duration_months is not None else ""}"></label>'
        "<label>Dates (text, e.g. Jun 2024 - Aug 2024)<br>"
        f'<input type="text" name="__PREFIX__dates" value="{html.escape(entry.dates or "")}"></label>'
        "<label>Tech (comma separated)<br>"
        f'<input type="text" name="__PREFIX__tech" value="{html.escape(tech)}"></label>'
        "<label>Description (one line per bullet)<br>"
        f'<textarea name="__PREFIX__description" rows="3">{html.escape(description)}</textarea></label>'
        '<button type="button" data-remove-row>Remove</button>'
        "</fieldset>"
    )


def _experience_rows(entries: list[Experience], prefix: str) -> str:
    if not entries:
        return ""
    return "".join(_experience_row(entry).replace("__PREFIX__", prefix) for entry in entries)


def _project_row(entry: Project | None) -> str:
    entry = entry or Project()
    tech = ", ".join(entry.tech)
    return (
        "<fieldset data-row>"
        "<label>Title<br>"
        f'<input type="text" name="project_title" value="{html.escape(entry.title or "")}"></label>'
        "<label>Summary<br>"
        f'<textarea name="project_summary" rows="2">{html.escape(entry.summary or "")}</textarea></label>'
        "<label>Outcomes<br>"
        f'<textarea name="project_outcomes" rows="2">{html.escape(entry.outcomes or "")}</textarea></label>'
        "<label>Tech (comma separated)<br>"
        f'<input type="text" name="project_tech" value="{html.escape(tech)}"></label>'
        '<button type="button" data-remove-row>Remove</button>'
        "</fieldset>"
    )


def _project_rows(entries: list[Project]) -> str:
    return "".join(_project_row(entry) for entry in entries)


def _education_row(entry: Education | None) -> str:
    entry = entry or Education()
    expected = "yes" if entry.expected else ""
    year = entry.graduation_year if entry.graduation_year is not None else ""
    return (
        "<fieldset data-row>"
        "<label>Degree<br>"
        f'<input type="text" name="education_degree" value="{html.escape(entry.degree or "")}"></label>'
        "<label>Field<br>"
        f'<input type="text" name="education_field" value="{html.escape(entry.field or "")}"></label>'
        "<label>Institution<br>"
        f'<input type="text" name="education_institution" value="{html.escape(entry.institution or "")}"></label>'
        "<label>Graduation year<br>"
        f'<input type="text" name="education_year" value="{year}"></label>'
        "<label>Expected (not yet graduated) "
        f'<select name="education_expected"><option value=""></option>'
        f'<option value="yes"{(" selected" if expected else "")}>yes</option></select></label>'
        '<button type="button" data-remove-row>Remove</button>'
        "</fieldset>"
    )


def _education_rows(entries: list[Education]) -> str:
    return "".join(_education_row(entry) for entry in entries)


def _profile_page(
    profile: CandidateProfile, error: str | None = None, warning: str | None = None
) -> str:
    """Render the profile review/edit form (GET and re-render on validation)."""
    certifications = html.escape("\n".join(profile.certifications))
    parts = [_HEAD, _nav(), "<h1>Profile review</h1>\n"]
    if error:
        parts.append(f'<p class="error">{html.escape(error)}</p>\n')
    if warning:
        parts.append(f'<p class="notice">{html.escape(warning)}</p>\n')
    if len(profile.skills) < _MIN_SKILL_WARNING:
        parts.append(
            '<p class="notice">Only '
            f"{len(profile.skills)} skill(s) were extracted (fewer than "
            f"{_MIN_SKILL_WARNING}). Add more before approving.</p>\n"
        )
    parts.append(
        '<form method="post" action="/profile">'
        "<p><label>Experience level (fresher/intern/junior/mid/senior):<br>"
        f'<input type="text" name="level" value="{html.escape(profile.experience_level.value)}"></label></p>'
        "<p><label>Total experience (months):<br>"
        f'<input type="text" name="months" value="{profile.total_experience_months}"></label></p>'
        "<p><label>Skills (one per line, name:proficiency; optional third part "
        "name:proficiency:evidenced-by project or internship; add a fourth part "
        '":confirm" to list it without tech-list evidence):<br>'
        f'<textarea name="skills" rows="8">{html.escape(_skill_text(profile))}</textarea></label></p>'
        "<p><label>Target roles (comma separated):<br>"
        f'<input type="text" name="target_roles" value="{html.escape(", ".join(profile.target_roles))}"></label></p>'
        '<p><label>Locations (comma separated, or "anywhere"):<br>'
        f'<input type="text" name="locations" value="{html.escape(", ".join(profile.locations))}" required></label></p>'
        "<p><label>Ambiguities (comma separated):<br>"
        f'<input type="text" name="ambiguities" value="{html.escape(", ".join(profile.ambiguities))}"></label></p>'
        "<h2>Links</h2>\n"
        "<p><label>GitHub (http/https only):<br>"
        f'<input type="text" name="github" value="{html.escape(profile.links.github or "")}"></label></p>'
        "<p><label>LinkedIn (http/https only):<br>"
        f'<input type="text" name="linkedin" value="{html.escape(profile.links.linkedin or "")}"></label></p>'
        "<p><label>Portfolio (http/https only):<br>"
        f'<input type="text" name="portfolio" value="{html.escape(profile.links.portfolio or "")}"></label></p>'
        "<h2>Internships</h2>\n"
        '<div id="internship-list">'
        + _experience_rows(profile.internships, "internship_")
        + "</div>"
        '<button type="button" data-add-rows="internship-template" data-list="internship-list">Add internship</button>'
        '<template id="internship-template">'
        + _experience_row(None).replace("__PREFIX__", "internship_")
        + "</template>"
        "<h2>Work experience</h2>\n"
        '<div id="work-list">' + _experience_rows(profile.work, "work_") + "</div>"
        '<button type="button" data-add-rows="work-template" data-list="work-list">Add work experience</button>'
        '<template id="work-template">'
        + _experience_row(None).replace("__PREFIX__", "work_")
        + "</template>"
        "<h2>Projects</h2>\n"
        '<div id="project-list">' + _project_rows(profile.projects) + "</div>"
        '<button type="button" data-add-rows="project-template" data-list="project-list">Add project</button>'
        '<template id="project-template">' + _project_row(None) + "</template>"
        "<h2>Education</h2>\n"
        '<div id="education-list">' + _education_rows(profile.education) + "</div>"
        '<button type="button" data-add-rows="education-template" data-list="education-list">Add education</button>'
        '<template id="education-template">' + _education_row(None) + "</template>"
        "<h2>Certifications</h2>\n"
        "<p><label>Certifications (one per line or comma separated):<br>"
        f'<textarea name="certifications" rows="4">{certifications}</textarea></label></p>'
        '<button type="submit">Approve profile</button></form>'
    )
    parts.append(_SECTION_SCRIPT)
    parts.append(_TABLE_FOOT)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def create_app(
    database: Path | None = None,
    host: str | None = None,
    client_factory: Callable[[], LLMClient] | None = None,
) -> FastAPI:
    """Build the dashboard ASGI app (``database`` defaults to ``ATLAS_DB``).

    ``client_factory`` is the LLM client used to draft a kit; it defaults to the
    CLI's client and is injectable so tests never touch the network.
    """
    if host and not host.startswith(("127.0.0.1", "localhost", "::1")) and _get_password() is None:
        raise RuntimeError("DASHBOARD_PASSWORD must be set when binding a non-loopback address")
    _recover_runs(database)
    app = FastAPI(title="Atlas dashboard", version="0.2.0")

    def _make_kit_client() -> LLMClient:
        if client_factory is not None:
            return client_factory()
        from .cli import _make_client

        return _make_client()

    def require_auth(request: Request) -> Response | None:
        if _get_password() is not None and not _check_auth(request):
            return RedirectResponse(url="/login", status_code=303)
        return None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request) -> Response:
        if _check_auth(request):
            return RedirectResponse(url="/", status_code=303)
        return HTMLResponse(_login_form())

    @app.post("/login")
    def login_post(request: Request, password: str = Form(...)) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        expected = _get_password()
        if expected is None:
            return RedirectResponse(url="/", status_code=303)
        if _constant_time_equal(password, expected):
            token = secrets.token_urlsafe(32)
            _sessions[token] = time.time()
            response = RedirectResponse(url="/", status_code=303)
            response.set_cookie(
                "atlas_session",
                token,
                httponly=True,
                samesite="lax",
                max_age=_SESSION_TTL,
            )
            return response
        return HTMLResponse(_login_form("Invalid password."), status_code=401)

    @app.get("/logout")
    def logout(request: Request) -> Response:
        token = request.cookies.get("atlas_session")
        if token:
            _sessions.pop(token, None)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("atlas_session")
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        try:
            rows = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(_page([], [], notice=f"Database unavailable: {exc}"))
        try:
            matched = shippable_jobs(rows)
        except sqlite3.Error as exc:
            return HTMLResponse(_page([], [], notice=f"Database unavailable: {exc}"))
        finally:
            rows.close()
        visible = [row for row in matched if str(row["source"] or "").lower() not in _TEST_SOURCES]
        decided: list[sqlite3.Row] = []
        undecided: list[sqlite3.Row] = []
        max_age = get_settings().filters.max_job_age_days
        now = datetime.now(UTC)
        for row in visible:
            outcome = str(_load_json(row["decision_json"]).get("outcome") or "").lower()
            if outcome in _SHOWN_OUTCOMES:
                decided.append(row)
            elif not outcome and _undecided_visible(row, max_age, now):
                # passed every hard filter but no decision row was ever stored
                undecided.append(row)
            # a stored SKIP is decided; it is neither shown nor "not yet evaluated"
        return HTMLResponse(_page(decided, undecided))

    @app.post("/jobs/{job_id}/status/{status}")
    def update_status(request: Request, job_id: int, status: str) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        try:
            set_application_status(conn, job_id, status)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        finally:
            conn.close()
        return RedirectResponse(url="/", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_result_get(request: Request, job_id: int) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(_job_error_page(f"Database unavailable: {exc}"), status_code=503)
        try:
            row = shippable_job(conn, job_id)
            verdict = latest_owner_verdict(conn, job_id) if row is not None else None
        finally:
            conn.close()
        if row is None:
            return HTMLResponse(
                _job_error_page("Job not found, or it did not pass every hard filter."),
                status_code=404,
            )
        return HTMLResponse(_job_result_page(row, owner_verdict=verdict))

    @app.post("/jobs/{job_id}/verdict")
    def job_verdict_post(
        request: Request,
        job_id: int,
        verdict: str = Form(...),
        reason: str = Form(""),
    ) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        try:
            row = shippable_job(conn, job_id)
            if row is None:
                return JSONResponse({"error": "job not found"}, status_code=404)
            system_decision = str(_load_json(row["decision_json"]).get("outcome") or "") or None
            save_owner_verdict(conn, job_id, verdict, reason.strip() or None, system_decision)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        finally:
            conn.close()
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    def _kit_job_or_error(job_id: int) -> tuple[sqlite3.Row | None, str | None, int]:
        """Load one APPLY/REVIEW job for the kit page, or an error page body."""
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return None, _kit_error_page(f"Database unavailable: {exc}"), 503
        try:
            row = shippable_job(conn, job_id)
        finally:
            conn.close()
        if row is None:
            return (
                None,
                _kit_error_page("Job not found, or it did not pass every hard filter."),
                404,
            )
        outcome = str(_load_json(row["decision_json"]).get("outcome") or "").lower()
        if outcome not in _DECISION_OUTCOMES:
            return None, _kit_error_page("No decision stored for this job."), 404
        return row, None, 200

    def _stored_kit(job_id: int) -> ApplicationKit | None:
        conn = _open(database)
        try:
            data = latest_kit_data(conn, job_id)
        finally:
            conn.close()
        if data is None:
            return None
        try:
            return ApplicationKit.model_validate(data)
        except ValueError:
            return None

    def _job_from_kit_row(row: sqlite3.Row) -> Job:
        return Job(
            source=str(row["source"] or ""),
            title=row["title"],
            company=row["company"],
            location=row["location"],
            url=str(row["url"] or ""),
            description_raw=str(row["description_raw"] or ""),
        )

    @app.get("/jobs/{job_id}/kit", response_class=HTMLResponse)
    def kit_get(request: Request, job_id: int) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        row, error_html, status = _kit_job_or_error(job_id)
        if row is None:
            return HTMLResponse(error_html, status_code=status)
        try:
            kit = _stored_kit(job_id)
        except sqlite3.Error:
            kit = None
        return HTMLResponse(_kit_page(row, kit=kit))

    @app.post("/jobs/{job_id}/kit")
    def kit_post(request: Request, job_id: int) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        row, error_html, status = _kit_job_or_error(job_id)
        if row is None:
            return HTMLResponse(error_html, status_code=status)
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(_kit_error_page(f"Database unavailable: {exc}"), status_code=503)
        try:
            profile = get_latest_profile(conn)
            if profile is None:
                return HTMLResponse(
                    _kit_error_page("Build and approve a profile first."), status_code=400
                )
            if not profile.approved:
                return HTMLResponse(
                    _kit_error_page("Approve your profile before generating a kit."),
                    status_code=400,
                )
            job = _job_from_kit_row(row)
            kit = generate_kit(_make_kit_client(), profile=profile, job=job)
            save_kit(
                conn,
                job_id,
                text=render_kit_text(kit),
                data_json=kit.model_dump_json(),
                model=kit.model,
                prompt_version=kit.prompt_version,
            )
        except LLMError as exc:
            return HTMLResponse(_kit_error_page(f"Kit generation failed: {exc}"), status_code=502)
        finally:
            conn.close()
        return HTMLResponse(_kit_page(row, kit=kit))

    @app.get("/api/matched")
    def api_matched(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        try:
            rows = matched_jobs(conn)
        finally:
            conn.close()
        return JSONResponse([dict(row) for row in rows])

    @app.get("/setup", response_class=HTMLResponse)
    def setup_get(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        body = (
            _HEAD
            + _nav()
            + "<h1>Setup</h1>\n"
            + "<p>Upload a resume PDF, describe yourself, or both. Either is enough.</p>\n"
            + '<form method="post" action="/setup" enctype="multipart/form-data">'
            + f"<p><label>Resume PDF (max {MAX_UPLOAD_MB} MB):<br>"
            + '<input type="file" name="resume" accept="application/pdf"></label></p>'
            + "<p><label>Description (interests, target roles, skills):<br>"
            + '<textarea name="description" rows="8"></textarea></label></p>'
            + '<button type="submit">Build profile</button></form>'
            + _TABLE_FOOT
        )
        return HTMLResponse(body)

    @app.post("/setup")
    def setup_post(
        request: Request,
        resume: Annotated[UploadFile | None, File()] = None,
        description: Annotated[str, Form()] = "",
    ) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied

        def problem(message: str) -> HTMLResponse:
            return HTMLResponse(
                _HEAD + _nav() + "<h1>Setup</h1>"
                f'<p class="error">{html.escape(message)}</p>' + _TABLE_FOOT
            )

        description = description.strip()
        saved: Path | None = None
        try:
            if resume is not None and resume.filename:
                if not resume.filename.lower().endswith(".pdf"):
                    return problem("Only PDF files are accepted.")
                data = resume.file.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    return problem(f"File too large: max {MAX_UPLOAD_MB} MB.")
                # Ignore the user's filename entirely; use a fixed, safe name.
                saved = DATA_DIR / "resume.pdf"
                saved.write_bytes(data)
            if saved is None and not description:
                return problem("Provide a resume PDF, a description, or both.")
            build_profile(
                resume_path=saved if saved is not None else None,
                description=description or None,
                database=database,
            )
        except Exception as exc:  # noqa: BLE001 - show the failure in the page
            return problem(f"Profile build failed: {exc}")
        finally:
            # Delete the uploaded resume once parsing is done (or on any error).
            if saved is not None and saved.exists():
                try:
                    saved.unlink()
                except OSError:
                    pass
        return RedirectResponse(url="/profile", status_code=303)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_get(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(
                _HEAD + _nav() + f'<p class="error">Database unavailable: {exc}</p>' + _TABLE_FOOT
            )
        try:
            profile = get_latest_profile(conn)
        finally:
            conn.close()
        if profile is None:
            return HTMLResponse(
                _HEAD
                + _nav()
                + "<h1>Profile</h1>"
                + '<p>No profile yet. Go to <a href="/setup">Setup</a>.</p>'
                + _TABLE_FOOT
            )
        return HTMLResponse(_profile_page(profile))

    @app.post("/profile")
    async def profile_post(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        form = await request.form()
        conn = _open(database)
        try:
            profile = get_latest_profile(conn) or CandidateProfile()
            level = str(form.get("level", ""))
            months = str(form.get("months", "0"))
            try:
                profile.experience_level = ExperienceLevel(level.strip())
            except ValueError:
                pass
            try:
                profile.total_experience_months = int(months.strip() or "0")
            except ValueError:
                pass
            profile.skills = _parse_skills(str(form.get("skills", "")))
            profile.target_roles = _split_items(str(form.get("target_roles", "")))
            profile.locations = _split_items(str(form.get("locations", "")))
            profile.ambiguities = _split_items(str(form.get("ambiguities", "")))
            # Richer facts the resume generator needs. Everything below comes
            # verbatim from the form; blank rows are dropped, never invented.
            profile.links, rejected_links = _parse_links(form)
            profile.internships = _parse_experiences(form, "internship_")
            profile.work = _parse_experiences(form, "work_")
            profile.projects = _parse_projects(form)
            profile.education = _parse_education(form)
            profile.certifications = _split_items(str(form.get("certifications", "")))
            # A profile cannot be approved without a location preference; the user
            # must name at least one location or type "anywhere". Nothing is
            # written, so the previous (unapproved or approved) row is untouched.
            if not profile.locations:
                return HTMLResponse(
                    _profile_page(
                        profile,
                        error='Add at least one location (or "anywhere") before approving.',
                    ),
                    status_code=422,
                )
            if rejected_links:
                return HTMLResponse(
                    _profile_page(
                        profile,
                        error=(
                            "Links must start with http:// or https://: "
                            + ", ".join(rejected_links)
                        ),
                    ),
                    status_code=422,
                )
            # Every internship, work and project entry needs real text the
            # generator can quote. Warn (and refuse to approve) when any is thin.
            missing = _entries_missing_text(profile.internships, profile.work, profile.projects)
            if missing:
                return HTMLResponse(
                    _profile_page(
                        profile,
                        warning=(
                            "Every internship, work and project entry needs a "
                            "description or outcome before approval. Missing: " + "; ".join(missing)
                        ),
                    ),
                    status_code=422,
                )
            profile.approved = True
            save_profile(conn, profile)
            from .config import settings_for_profile
            from .pipeline import reapply_filters

            reapply_filters(conn, profile, settings_for_profile(profile), reason="profile approved")
        finally:
            conn.close()
        return RedirectResponse(url="/", status_code=303)

    @app.get("/rejected", response_class=HTMLResponse)
    def rejected_get(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(
                _HEAD + _nav() + f'<p class="error">Database unavailable: {exc}</p>' + _TABLE_FOOT
            )
        try:
            rows = rejected_jobs(conn)
        finally:
            conn.close()
        parts = [
            _HEAD,
            _nav(),
            "<h1>Why not shown</h1>\n",
            "<p>Jobs with at least one failed hard filter.</p>\n",
            (
                "<table><thead><tr><th>#</th><th>Job</th><th>Company</th>"
                "<th>Rule</th><th>Reason</th><th>Recorded</th></tr></thead><tbody>\n"
            ),
        ]
        if rows:
            for row in rows:
                url = str(row["url"] or "")
                title = html.escape(str(row["title"] or row["url"] or f"job {row['id']}"))
                link = (
                    f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">{title}</a>'
                    if is_http_url(url)
                    else title
                )
                evidence_cell = html.escape(str(row["evidence"] or "-"))
                data = _load_json(row["data_json"])
                checks = data.get("checks", [])
                if checks:
                    evidence_cell += "<br>checked: " + "; ".join(
                        html.escape(_render_check(check)) for check in checks
                    )
                rule_cell = html.escape(str(row["rule_id"] or "-"))
                if data.get("cheap"):
                    rule_cell += ' <em title="decided without an LLM call">cheap</em>'
                parts.append(
                    f"<tr><td>{int(row['id'])}</td><td>{link}</td>"
                    f"<td>{html.escape(str(row['company'] or '-'))}</td>"
                    f"<td>{rule_cell}</td>"
                    f"<td>{evidence_cell}</td>"
                    f"<td>{html.escape(_recorded_text(row['filter_created_at']))}</td></tr>\n"
                )
        else:
            parts.append('<tr><td colspan="6">No rejected jobs.</td></tr>\n')
        parts.append("</table>\n")
        parts.append(_TABLE_FOOT)
        return HTMLResponse("".join(parts))

    @app.post("/find")
    def find_jobs(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        blocked = groq_daily_limit_message(database)
        if blocked is not None:
            # Do not start a run that would only hit the same wall: show the
            # reason and let the owner retry after the quota resets.
            logger.warning("find refused: %s", blocked)
            body = (
                _HEAD
                + _nav()
                + "<h1>Find jobs</h1>\n"
                + f'<p class="warn">{html.escape(blocked)}</p>\n'
                + '<p><a href="/status">Back to status</a></p>\n'
                + _TABLE_FOOT
            )
            return HTMLResponse(body)
        start_pipeline(database)
        return RedirectResponse(url="/status", status_code=303)

    @app.get("/add", response_class=HTMLResponse)
    def add_get(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        return HTMLResponse(_add_form())

    @app.post("/add")
    def add_post(
        request: Request,
        title: Annotated[str, Form()] = "",
        company: Annotated[str, Form()] = "",
        location: Annotated[str, Form()] = "",
        apply_url: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> Response:
        # A pasted job is evaluated through the exact same stages as a collected
        # one, but synchronously, so the owner sees the decision immediately.
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied

        title = title.strip()
        company = company.strip()
        location = location.strip()
        apply_url = apply_url.strip()
        description = description.strip()

        def form_error(message: str) -> HTMLResponse:
            return HTMLResponse(
                _add_form(
                    error=message,
                    title=title,
                    company=company,
                    location=location,
                    apply_url=apply_url,
                    description=description,
                ),
                status_code=422,
            )

        if not title:
            return form_error("Add a job title.")
        if not description:
            return form_error("Paste the job description text (required).")
        if len(description) > MAX_JD_CHARS:
            return form_error(f"Job description is too long: max {MAX_JD_CHARS:,} characters.")
        if apply_url and not is_http_url(apply_url):
            return form_error("The apply URL must be an http(s) link.")

        blocked = groq_daily_limit_message(database)
        if blocked is not None:
            logger.warning("add refused: %s", blocked)
            return HTMLResponse(
                _add_result_page(
                    body=f'<p class="warn">{html.escape(blocked)}</p>\n'
                    "<p>The job was not saved. Try again after the reset.</p>"
                )
            )

        if not _try_begin_single_run():
            return HTMLResponse(
                _add_result_page(
                    body='<p class="warn">A run is already in progress. '
                    "Try again when it finishes.</p>"
                )
            )

        try:
            try:
                conn = _open(database)
            except sqlite3.Error as exc:
                _end_single_run()
                return HTMLResponse(
                    _add_result_page(body=f'<p class="error">{html.escape(str(exc))}</p>')
                )
            try:
                profile = get_latest_profile(conn)
                if profile is None or not profile.approved:
                    conn.close()
                    _end_single_run()
                    return HTMLResponse(
                        _add_result_page(
                            body='<p class="error">No approved profile. Go to '
                            '<a href="/setup">Setup</a> first.</p>'
                        )
                    )

                from .config import settings_for_profile
                from .normalize import normalize_job

                settings = settings_for_profile(profile, get_settings())
                run_id = start_run(conn, "add")
                job = Job(
                    source="owner_added",
                    title=title or None,
                    company=company or None,
                    location=location or None,
                    url=apply_url,
                    description_raw=description,
                )
                job = normalize_job(job)
                job_id, created = upsert_job(conn, run_id, job)
                if not created:
                    finish_run(conn, run_id, "done", {"duplicate": True})
                    conn.close()
                    _end_single_run()
                    return HTMLResponse(_add_result_page(job_id=job_id, duplicate=True))

                # Hand off to a background thread: the POST returns immediately and
                # redirects to the progress page, which polls /api/add-status until
                # done. The worker opens its own connection (sqlite connections are
                # thread-affine) and owns the run lock and add state.
                client = _make_kit_client()
                with _add_lock:
                    _add_state.update(_blank_add_state())
                    _add_state["running"] = True
                    _add_state["job_id"] = job_id
                    _add_state["started_at"] = time.time()
                    _add_state["progress"] = {
                        "current_step": "Starting\u2026",
                        "llm_calls": 0,
                        "tokens_used": 0,
                    }
                conn.close()
                threading.Thread(
                    target=_run_add_job,
                    args=(database, run_id, job_id, job, profile, settings, client),
                    daemon=True,
                ).start()
                return RedirectResponse(url="/add/progress", status_code=303)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    logger.debug("conn close failed during error path", exc_info=True)
                _end_single_run()
                raise
        except sqlite3.Error as exc:
            return HTMLResponse(
                _add_result_page(body=f'<p class="error">{html.escape(str(exc))}</p>')
            )

    @app.get("/add/progress", response_class=HTMLResponse)
    def add_progress_get(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        with _add_lock:
            done = bool(_add_state["done"])
        # Already finished before the browser asked: render the result directly.
        if done:
            return HTMLResponse(_add_result_page_view())
        return HTMLResponse(_add_progress_page())

    @app.get("/api/add-status")
    def add_status_get(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        with _add_lock:
            snapshot = json.loads(json.dumps(_add_state))
        progress = snapshot.get("progress") or {}
        return JSONResponse(
            {
                "running": snapshot.get("running", False),
                "done": snapshot.get("done", False),
                "job_id": snapshot.get("job_id"),
                "paused": snapshot.get("paused"),
                "error": snapshot.get("error"),
                "current_step": progress.get("current_step"),
                "rate_limit_wait": progress.get("rate_limit_wait"),
                "rate_limit_reason": progress.get("rate_limit_reason"),
                "llm_calls": progress.get("llm_calls"),
                "tokens_used": progress.get("tokens_used"),
                "started_at": snapshot.get("started_at"),
                "finished_at": snapshot.get("finished_at"),
            }
        )

    @app.get("/status", response_class=HTMLResponse)
    def status_get(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        status = _pipeline_status
        counts = (
            "".join(
                f"<li>{html.escape(str(k))}: {html.escape(str(v))}</li>"
                for k, v in (status.get("counts") or {}).items()
            )
            or "<li>-</li>"
        )
        stages = (
            "".join(
                f"<li>{html.escape(str(k))}: {html.escape(str(v))}</li>"
                for k, v in (status.get("stages") or {}).items()
            )
            or "<li>-</li>"
        )
        source_items = status.get("sources") or []
        if source_items:
            sources = "".join(
                "<li>"
                + html.escape(str(item.get("source")))
                + f": fetched={html.escape(str(item.get('fetched')))}"
                + f" kept={html.escape(str(item.get('kept')))}"
                + f" passed={html.escape(str(item.get('passed_filters')))}"
                + f" sent={html.escape(str(item.get('sent')))}"
                + "</li>"
                for item in source_items
            )
        else:
            sources = "<li>-</li>"
        errors = (
            "".join(
                f"<li>{html.escape(str(k))}: {html.escape(str(v))}</li>"
                for k, v in (status.get("errors") or {}).items()
            )
            or "<li>-</li>"
        )
        started = status.get("started_at")
        if started:
            end = status.get("finished_at") or time.time()
            elapsed = f"{end - started:.0f}s"
        else:
            elapsed = "-"
        current = status.get("current_source") or "-"
        prog = status.get("progress") or status.get("stages") or {}

        def _scalar(key: str) -> str:
            value = prog.get(key)
            return html.escape(str(value)) if value not in (None, "") else "-"

        current_job = "-"
        job_id = prog.get("current_job_id")
        job_title = prog.get("current_job_title")
        if job_id is not None:
            label = f"{job_id} — {job_title}" if job_title else str(job_id)
            current_job = html.escape(label)
        current_step = _scalar("current_step")
        calls_used = prog.get("llm_calls")
        calls_cap = prog.get("llm_calls_cap")
        if calls_cap is not None:
            llm_calls = html.escape(f"{calls_used if calls_used is not None else 0} / {calls_cap}")
        else:
            llm_calls = "-"
        waiting = ""
        wait_seconds = prog.get("rate_limit_wait")
        if status.get("running") and wait_seconds:
            reason = prog.get("rate_limit_reason") or "rate limit"
            waiting = (
                '<p class="warn">Waiting for Groq '
                f"{html.escape(str(reason))}, retry in {html.escape(str(wait_seconds))}s…</p>\n"
            )
        paused = ""
        if status.get("stage") == "paused" or prog.get("paused"):
            message = prog.get("pause_message") or "Paused"
            paused = f'<p class="warn">{html.escape(str(message))}</p>\n'
        try:
            _conn = _open(database)
            try:
                tokens_today = tokens_used_today(_conn)
            finally:
                _conn.close()
        except sqlite3.Error:
            tokens_today = 0
        tokens_line = (
            f"<p>Tokens used today: ~{html.escape(f'{tokens_today:,}')} "
            f"(this run: {html.escape(str(prog.get('tokens_used') or 0))})</p>\n"
        )
        eta = "-"
        total = prog.get("total")
        done = prog.get("done")
        if started and isinstance(total, int) and isinstance(done, int) and 0 < done < total:
            per_job = (time.time() - started) / done
            eta = f"~{per_job * (total - done):.0f}s"
        recovered = ""
        if status.get("recovered_run_id") is not None and not status.get("running"):
            recovered = (
                f"<p>Showing last recorded run #{html.escape(str(status['recovered_run_id']))} "
                f"(result: {html.escape(str(status.get('result') or '-'))}) — "
                "restored from the database after a restart.</p>\n"
            )
        live = (
            f"<p>Current job: {current_job} &nbsp; "
            f"Step: {current_step} &nbsp; "
            f"LLM calls: {llm_calls} &nbsp; "
            f"ETA: {html.escape(eta)}</p>\n"
        )
        body = (
            _HEAD
            + _nav()
            + "<h1>Pipeline status</h1>\n"
            + f"<p>Running: {html.escape(str(status['running']))} &nbsp; "
            + f"Stage: {html.escape(str(status['stage']))} &nbsp; "
            + f"Current source: {html.escape(str(current))} &nbsp; "
            + f"Elapsed: {html.escape(elapsed)}</p>\n"
            + live
            + tokens_line
            + waiting
            + paused
            + recovered
            + f"<p>Per-stage counts:</p><ul>{stages}</ul>\n"
            + f"<p>Per-source results:</p><ul>{sources}</ul>\n"
            + f"<p>Totals in database:</p><ul>{counts}</ul>\n"
            + f"<p>Errors per source:</p><ul>{errors}</ul>\n"
            + '<p><form method="post" action="/find">'
            + '<button type="submit">Find jobs</button></form></p>\n'
            + '<p><a href="/">Back to results</a></p>\n'
            + "<script>setTimeout(function(){location.reload();}, 3000);</script>\n"
            + _TABLE_FOOT
        )
        return HTMLResponse(body)

    @app.get("/api/status")
    def api_status(request: Request) -> Response:
        denied = require_auth(request)
        if denied:
            return denied
        return JSONResponse(_pipeline_status)

    return app
