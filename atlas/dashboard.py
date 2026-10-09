"""Local dashboard for matched jobs and their application status.

Served by ``python -m atlas.cli dashboard`` (or ``uvicorn app:app``). One
server-rendered HTML page plus a JSON view — no JavaScript framework, no
template engine, no outbound requests.

``APLD`` / ``NTAPLD`` / ``PEND`` are manual bookkeeping: the dashboard only
stores what a human clicked, it never applies to a job anywhere.
"""

from __future__ import annotations

import html
import hmac
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .db import APPLICATION_STATUSES, connect, init_db, matched_jobs, set_application_status
from .digest import is_http_url

_LABELS = {"APLD": "Applied", "NTAPLD": "Not applied", "PEND": "Pending"}
_COLORS = {"APLD": "#1a7f37", "NTAPLD": "#cf222e", "PEND": "#9a6700"}

_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Atlas — dashboard</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1f2328; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 0.45rem 0.6rem; text-align: left; }
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
nav a { margin-right: 1rem; }
textarea { width: 100%; }
</style>
</head>
<body>
"""

_TABLE_HEAD = """<table>
<thead><tr><th>#</th><th>Job</th><th>Company</th><th>Location</th>
<th>Pipeline</th><th>Apply status</th><th>Set</th></tr></thead>
<tbody>
"""

_TABLE_FOOT = """</tbody>
</table>
</body>
</html>
"""

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_MB = 5
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

_pipeline_lock = threading.Lock()
_pipeline_status: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "counts": {"collected": 0, "parsed": 0, "filtered": 0, "evaluated": 0, "verified": 0, "ranked": 0, "decided": 0},
    "errors": {},
    "started_at": None,
    "finished_at": None,
    "result": None,
}

_sessions: dict[str, float] = {}
_SESSION_TTL = 3600 * 24 * 7


def _get_password() -> str | None:
    return os.environ.get("DASHBOARD_PASSWORD")


def _constant_time_equal(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return a == b


def _login_form(error: str | None = None) -> str:
    err = f'<p class="error">{html.escape(error)}</p>\n' if error else ""
    return (
        _HEAD
        + "<h1>Atlas — login</h1>\n"
        + err
        + '<form method="post" action="/login">'
        + '<input type="password" name="password" placeholder="Password" required>'
        + '<button type="submit">Login</button>'
        + "</form>"
        + _TABLE_FOOT.replace("</table></body></html>", "</body></html>")
    )


def _nav() -> str:
    return (
        '<nav><a href="/">Home</a><a href="/setup">Setup</a><a href="/profile">Profile</a>'
        '<a href="/rejected">Rejected</a><a href="/status">Status</a></nav>'
    )


def _check_auth(request: Request) -> bool:
    session = request.cookies.get("atlas_session")
    if session and session in _sessions:
        _sessions[session] = time.time()
        return True
    return False




def _row_html(row: sqlite3.Row) -> str:
    job_id = int(row["id"])
    url = str(row["url"] or "")
    title = html.escape(str(row["title"] or url or f"job {job_id}"))
    job_cell = (
        f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">{title}</a>'
        if is_http_url(url)
        else title
    )
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
        f"<td>{html.escape(str(row['status'] or '-'))}</td>"
        f'<td><span class="badge" style="background:{_COLORS[current]}">'
        f"{_LABELS[current]}</span></td>"
        f'<td class="actions">{actions}</td></tr>\n'
    )


def _page(rows: list[sqlite3.Row], notice: str | None = None) -> str:
    parts = [_HEAD, "<h1>Atlas — matched jobs</h1>\n"]
    if notice:
        parts.append(f'<p class="notice">{html.escape(notice)}</p>\n')
    parts.append(
        f"<p>{len(rows)} matched job(s). Apply status is manual bookkeeping "
        "(APLD = applied, NTAPLD = not applied, PEND = pending).</p>\n"
    )
    parts.append(_TABLE_HEAD)
    if rows:
        parts.extend(_row_html(row) for row in rows)
    else:
        parts.append('<tr><td colspan="7">No matched jobs yet.</td></tr>\n')
    parts.append(_TABLE_FOOT)
    return "".join(parts)


def _open(database: Path | None) -> sqlite3.Connection:
    conn = connect(database)
    init_db(conn)
    return conn


def _same_origin(request: Request) -> bool:
    """CSRF guard: reject POSTs whose Origin/Referer points at another site.

    A request without either header (curl, the test client) carries no
    ambient browser authority and is allowed.
    """
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if candidate is None:
        return True
    return urlparse(candidate).netloc == request.url.netloc


def create_app(database: Path | None = None) -> FastAPI:
    """Build the dashboard ASGI app (``database`` defaults to ``ATLAS_DB``)."""
    pwd = _get_password()
    app = FastAPI(title="Atlas dashboard", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request) -> HTMLResponse:
        if _check_auth(request):
            return RedirectResponse(url="/", status_code=303)
        return HTMLResponse(_login_form())

    @app.post("/login")
    def login_post(request: Request, password: str = Form(...)) -> Response:
        pwd = _get_password()
        if pwd is None:
            return RedirectResponse(url="/login", status_code=303)
        if _constant_time_equal(password, pwd):
            sid = secrets.token_urlsafe(32)
            _sessions[sid] = time.time()
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie("atlas_session", sid, httponly=True, secure=False, samesite="lax", max_age=_SESSION_TTL)
            return resp
        return HTMLResponse(_login_form("Invalid password"), status_code=401)

    @app.get("/logout")
    def logout(request: Request) -> Response:
        sid = request.cookies.get("atlas_session")
        if sid in _sessions:
            _sessions.pop(sid, None)
        resp = RedirectResponse(url="/login", status_code=303)
        resp.delete_cookie("atlas_session")
        return resp

    def _require_auth(request: Request) -> Response | None:
        pwd = _get_password()
        if pwd is not None and not _check_auth(request):
            return RedirectResponse(url="/login", status_code=303)
        return None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        auth = _require_auth(request)
        if auth:
            return auth
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(_page([], notice=f"Database unavailable: {exc}"))
        try:
            rows = matched_jobs(conn)
        finally:
            conn.close()
        body = _page(rows)
        return HTMLResponse(body)

    @app.post("/jobs/{job_id}/status/{status}")
    def update_status(request: Request, job_id: int, status: str) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        auth = _require_auth(request)
        if auth:
            return auth
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
        auth = _require_auth(request)
        if auth:
            return auth
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        try:
            rows = matched_jobs(conn)
        finally:
            conn.close()
        return JSONResponse([dict(row) for row in rows])

    return app
