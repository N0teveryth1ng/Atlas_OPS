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
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .db import (
    APPLICATION_STATUSES,
    connect,
    get_latest_profile,
    init_db,
    matched_jobs,
    rejected_jobs,
    save_profile,
    set_application_status,
    shippable_jobs,
)
from .digest import is_http_url
from .profile_agent import build_profile as _build_profile_impl
from .profile_agent import normalize_skill_name
from .schemas import CandidateProfile, ExperienceLevel, Proficiency, Seniority, Skill

_LABELS = {"APLD": "Applied", "NTAPLD": "Not applied", "PEND": "Pending"}
_COLORS = {"APLD": "#1a7f37", "NTAPLD": "#cf222e", "PEND": "#9a6700"}
_MIN_SKILL_WARNING = 5
#: Outcomes the results table may show; SKIP is decided but never shown here.
_SHOWN_OUTCOMES = frozenset({"apply", "review"})
#: Non-real rows (test fixtures inserted by the pipeline tests) kept off the page.
_TEST_SOURCES = frozenset({"manual"})

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 5
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
_SESSION_TTL = 3600 * 24 * 7

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
        "started_at": None,
        "finished_at": None,
        "result": None,
    }


_pipeline_status: dict[str, Any] = _blank_pipeline_status()

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
        '<nav><a href="/">Home</a><a href="/setup">Setup</a><a href="/profile">Profile</a>'
        '<a href="/rejected">Rejected</a><a href="/status">Status</a></nav>'
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


def _run_pipeline_worker(database: Path | None = None) -> None:
    """Collect and process jobs using the exact CLI run path, reporting progress."""
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
            elif stage == "processing":
                _pipeline_status["stage"] = "processing"
                _pipeline_status["stages"] = {
                    key: value for key, value in event.items() if key != "stage"
                }
                _pipeline_status["progress"] = _pipeline_status["stages"]
            elif stage == "sources":
                _pipeline_status["sources"] = list(event.get("sources") or [])

        args = argparse.Namespace(collect=True, limit=50)
        code = _execute_run(args, run_kind="dashboard", on_event=on_event)
        _pipeline_status["counts"] = _db_counts(database)
        if code == 0:
            _pipeline_status["stage"] = "done"
            _pipeline_status["result"] = "ok"
        else:
            _pipeline_status["stage"] = "error"
            _pipeline_status["errors"]["pipeline"] = f"exit code {code}"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the page
        _pipeline_status["stage"] = "error"
        _pipeline_status["errors"]["pipeline"] = str(exc)
    finally:
        _pipeline_status["running"] = False
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
    return (
        f"<tr><td>{job_id}</td><td>{job_cell}</td>"
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
        f'<td class="actions">{actions}</td></tr>\n'
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
<th>Source</th><th>Seniority</th><th>Why</th></tr></thead>
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
        f"<tr><td>{job_id}</td><td>{job_cell}</td>"
        f"<td>{html.escape(str(row['company'] or '-'))}</td>"
        f"<td>{html.escape(str(row['location'] or '-'))}</td>"
        f"<td>{html.escape(str(row['source'] or '-'))}</td>"
        f"<td>{html.escape(_seniority_text(evaluation, parsed))}</td>"
        "<td>passed all filters, no decision stored yet</td></tr>\n"
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
        name, _, prof = entry.partition(":")
        name = name.strip()
        if not name:
            continue
        try:
            proficiency = Proficiency(prof.strip().lower())
        except ValueError:
            proficiency = Proficiency.familiar
        skills.append(
            Skill(name=name, canonical_name=normalize_skill_name(name), proficiency=proficiency)
        )
    return skills


def _profile_page(profile: CandidateProfile, error: str | None = None) -> str:
    """Render the profile review/edit form (GET and re-render on validation)."""
    skills = "\n".join(
        f"{s.name}:{getattr(s.proficiency, 'value', s.proficiency)}" for s in profile.skills
    )
    parts = [_HEAD, _nav(), "<h1>Profile review</h1>\n"]
    if error:
        parts.append(f'<p class="error">{html.escape(error)}</p>\n')
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
        "<p><label>Skills (one per line, name:proficiency):<br>"
        f'<textarea name="skills" rows="8">{html.escape(skills)}</textarea></label></p>'
        "<p><label>Target roles (comma separated):<br>"
        f'<input type="text" name="target_roles" value="{html.escape(", ".join(profile.target_roles))}"></label></p>'
        '<p><label>Locations (comma separated, or "anywhere"):<br>'
        f'<input type="text" name="locations" value="{html.escape(", ".join(profile.locations))}" required></label></p>'
        "<p><label>Ambiguities (comma separated):<br>"
        f'<input type="text" name="ambiguities" value="{html.escape(", ".join(profile.ambiguities))}"></label></p>'
        '<button type="submit">Approve profile</button></form>'
    )
    parts.append(_TABLE_FOOT)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #


def create_app(database: Path | None = None, host: str | None = None) -> FastAPI:
    """Build the dashboard ASGI app (``database`` defaults to ``ATLAS_DB``)."""
    if host and not host.startswith(("127.0.0.1", "localhost", "::1")) and _get_password() is None:
        raise RuntimeError("DASHBOARD_PASSWORD must be set when binding a non-loopback address")
    app = FastAPI(title="Atlas dashboard", version="0.2.0")

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
        for row in visible:
            outcome = str(_load_json(row["decision_json"]).get("outcome") or "").lower()
            if outcome in _SHOWN_OUTCOMES:
                decided.append(row)
            elif not outcome:
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
    def profile_post(
        request: Request,
        level: str = Form(""),
        months: str = Form("0"),
        skills: str = Form(""),
        target_roles: str = Form(""),
        locations: str = Form(""),
        ambiguities: str = Form(""),
    ) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        denied = require_auth(request)
        if denied:
            return denied
        conn = _open(database)
        try:
            profile = get_latest_profile(conn) or CandidateProfile()
            try:
                profile.experience_level = ExperienceLevel(level.strip())
            except ValueError:
                pass
            try:
                profile.total_experience_months = int(months.strip() or "0")
            except ValueError:
                pass
            profile.skills = _parse_skills(skills)
            profile.target_roles = [r.strip() for r in target_roles.split(",") if r.strip()]
            profile.locations = [item.strip() for item in locations.split(",") if item.strip()]
            profile.ambiguities = [item.strip() for item in ambiguities.split(",") if item.strip()]
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
            profile.approved = True
            save_profile(conn, profile)
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
                "<th>Rule</th><th>Reason</th></tr></thead><tbody>\n"
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
                checks = _load_json(row["data_json"]).get("checks", [])
                if checks:
                    evidence_cell += "<br>checked: " + "; ".join(
                        html.escape(_render_check(check)) for check in checks
                    )
                parts.append(
                    f"<tr><td>{int(row['id'])}</td><td>{link}</td>"
                    f"<td>{html.escape(str(row['company'] or '-'))}</td>"
                    f"<td>{html.escape(str(row['rule_id'] or '-'))}</td>"
                    f"<td>{evidence_cell}</td></tr>\n"
                )
        else:
            parts.append('<tr><td colspan="5">No rejected jobs.</td></tr>\n')
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
        start_pipeline(database)
        return RedirectResponse(url="/status", status_code=303)

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
        body = (
            _HEAD
            + _nav()
            + "<h1>Pipeline status</h1>\n"
            + f"<p>Running: {html.escape(str(status['running']))} &nbsp; "
            + f"Stage: {html.escape(str(status['stage']))}</p>\n"
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
