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
try:
    from .profile_agent import build_profile
except Exception:  # pragma: no cover
    build_profile = None
try:
    from .pipeline import run_pipeline
except Exception:  # pragma: no cover
    run_pipeline = None

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
<thead><tr><th>#</th><th>Title</th><th>Company</th><th>Location</th><th>Source</th>
<th>Apply link</th><th>Score</th><th>Decision</th><th>Top 2 reasons for</th>
<th>Main risk</th><th>Missing skills</th><th>Seniority assessment</th>
<th>Application status</th><th>Set</th></tr></thead>
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
    apply_link = (
        f'<a href="{html.escape(url, quote=True)}" rel="noreferrer">Apply</a>'
        if is_http_url(url)
        else "-"
    )
    current = str(row.get("application_status") or "PEND")
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
    score = row.get("score") if "score" in row.keys() else row.get("fit_score")
    decision = row.get("decision") if "decision" in row.keys() else "-"
    reasons = row.get("reasons_for") if "reasons_for" in row.keys() else row.get("reasons")
    risk = row.get("main_risk") if "main_risk" in row.keys() else "-"
    missing = row.get("missing_skills") if "missing_skills" in row.keys() else "-"
    seniority = row.get("seniority_assessment") if "seniority_assessment" in row.keys() else "-"
    return (
        f"<tr><td>{job_id}</td><td>{title}</td>"
        f"<td>{html.escape(str(row.get('company') or '-'))}</td>"
        f"<td>{html.escape(str(row.get('location') or '-'))}</td>"
        f"<td>{html.escape(str(row.get('source') or '-'))}</td>"
        f"<td>{apply_link}</td>"
        f"<td>{html.escape(str(score or '-'))}</td>"
        f"<td>{html.escape(str(decision or '-'))}</td>"
        f"<td>{html.escape(str(reasons or '-'))}</td>"
        f"<td>{html.escape(str(risk or '-'))}</td>"
        f"<td>{html.escape(str(missing or '-'))}</td>"
        f"<td>{html.escape(str(seniority or '-'))}</td>"
        f'<td><span class="badge" style="background:{_COLORS[current]}">{_LABELS[current]}</span></td>'
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


def create_app(database: Path | None = None, host: str | None = None) -> FastAPI:
    """Build the dashboard ASGI app (``database`` defaults to ``ATLAS_DB``)."""
    pwd = _get_password()
    if host and not host.startswith("127.0.0.1") and not host.startswith("localhost") and not host.startswith("::1"):
        if pwd is None:
            raise RuntimeError("DASHBOARD_PASSWORD must be set when binding non-loopback address")
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
        # filter to APPLY or REVIEW
        filtered = []
        for r in rows:
            dec = str(r["decision"] or "").upper() if "decision" in r.keys() else ""
            if dec in ("APPLY", "REVIEW"):
                filtered.append(r)
        body = _HEAD + _nav() + "<h1>Atlas — matched jobs</h1>"
        body += "<p><form method='post' action='/find'><button type='submit'>Find jobs</button></form></p>"
        body += "<p>Showing only jobs with decision APPLY or REVIEW.</p>"
        body += _TABLE_HEAD
        if filtered:
            for fr in filtered:
                body += _row_html(fr)
        else:
            body += '<tr><td colspan="14">No matched jobs yet.</td></tr>\n'
        body += _TABLE_FOOT
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

    @app.get("/setup", response_class=HTMLResponse)
    def setup_get(request: Request) -> Response:
        auth = _require_auth(request)
        if auth:
            return auth
        content = (
            _HEAD
            + _nav()
            + "<h1>Setup</h1>"
            + "<form method='post' action='/setup' enctype='multipart/form-data'>"
            + "<label>Upload resume PDF (max 5MB, pdf only):</label><br>"
            + "<input type='file' name='resume' accept='.pdf,application/pdf'><br><br>"
            + "<label>Description (either or both):</label><br>"
            + "<textarea name='description' rows='6'></textarea><br><br>"
            + "<button type='submit'>Build profile</button>"
            + "</form>"
            + _TABLE_FOOT
        )
        return HTMLResponse(content)

    @app.post("/setup")
    async def setup_post(request: Request, resume: UploadFile | None = File(None), description: str = Form("")) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        auth = _require_auth(request)
        if auth:
            return auth
        desc = (description or "").strip()
        resume_path = None
        try:
            if resume is not None and resume.filename:
                name = Path(resume.filename).name
                if not name.lower().endswith(".pdf"):
                    return HTMLResponse(_HEAD + _nav() + "<h1>Setup</h1><p class='error'>PDF only.</p>" + _TABLE_FOOT)
                data = await resume.read()
                if len(data) > MAX_UPLOAD_BYTES:
                    return HTMLResponse(_HEAD + _nav() + "<h1>Setup</h1><p class='error'>File too large (max 5MB).</p>" + _TABLE_FOOT)
                resume_path = DATA_DIR / "resume.pdf"
                with open(resume_path, "wb") as f:
                    f.write(data)
            if not desc and resume_path is None:
                return HTMLResponse(_HEAD + _nav() + "<h1>Setup</h1><p class='error'>Provide resume or description.</p>" + _TABLE_FOOT)
            if build_profile is not None:
                try:
                    build_profile(resume_path=str(resume_path) if resume_path else None, description=desc or None, yes=True, database=database)
                except Exception as e:
                    if resume_path and resume_path.exists():
                        try:
                            resume_path.unlink()
                        except Exception:
                            pass
                    return HTMLResponse(_HEAD + _nav() + f"<h1>Setup</h1><p class='error'>Profile build failed: {html.escape(str(e))}</p>" + _TABLE_FOOT)
            if resume_path and resume_path.exists():
                try:
                    resume_path.unlink()
                except Exception:
                    pass
            return RedirectResponse(url="/profile", status_code=303)
        except Exception as e:
            if resume_path and resume_path.exists():
                try:
                    resume_path.unlink()
                except Exception:
                    pass
            return HTMLResponse(_HEAD + _nav() + f"<h1>Setup</h1><p class='error'>Error: {html.escape(str(e))}</p>" + _TABLE_FOOT)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_get(request: Request) -> Response:
        auth = _require_auth(request)
        if auth:
            return auth
        try:
            conn = _open(database)
            row = conn.execute("SELECT * FROM profiles ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
        except Exception as e:
            return HTMLResponse(_HEAD + _nav() + f"<h1>Profile</h1><p class='error'>{html.escape(str(e))}</p>" + _TABLE_FOOT)
        if not row:
            return HTMLResponse(_HEAD + _nav() + "<h1>Profile</h1><p>No profile found. Go to <a href='/setup'>Setup</a>.</p>" + _TABLE_FOOT)
        skills = row["skills"] if "skills" in row.keys() else ""
        skill_count = len([s for s in str(skills).split(",") if s.strip()]) if skills else 0
        warn = ""
        if skill_count < 5:
            warn = "<p class='notice'>Warning: fewer than 5 skills extracted. Add skills manually below.</p>"
        content = (
            _HEAD
            + _nav()
            + "<h1>Profile</h1>"
            + warn
            + "<form method='post' action='/profile'>"
            + f"<label>Level:</label><br><input name='level' value='{html.escape(str(row['level'] or ''))}'><br>"
            + f"<label>Years:</label><br><input name='years' value='{html.escape(str(row['years'] or ''))}'><br>"
            + f"<label>Skills (comma separated):</label><br><input name='skills' value='{html.escape(str(skills))}' style='width:100%'><br>"
            + f"<label>Target roles:</label><br><input name='target_roles' value='{html.escape(str(row['target_roles'] or ''))}' style='width:100%'><br>"
            + f"<label>Locations:</label><br><input name='locations' value='{html.escape(str(row['locations'] or ''))}' style='width:100%'><br>"
            + f"<label>Ambiguities:</label><br><input name='ambiguities' value='{html.escape(str(row['ambiguities'] or ''))}' style='width:100%'><br><br>"
            + "<button type='submit'>Approve</button>"
            + "</form>"
            + _TABLE_FOOT
        )
        return HTMLResponse(content)

    @app.post("/profile")
    def profile_post(request: Request, level: str = Form(""), years: str = Form(""), skills: str = Form(""), target_roles: str = Form(""), locations: str = Form(""), ambiguities: str = Form("")) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        auth = _require_auth(request)
        if auth:
            return auth
        try:
            conn = _open(database)
            row = conn.execute("SELECT id FROM profiles ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                conn.execute(
                    "UPDATE profiles SET level=?, years=?, skills=?, target_roles=?, locations=?, ambiguities=?, approved_at=CURRENT_TIMESTAMP WHERE id=?",
                    (level, years, skills, target_roles, locations, ambiguities, row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO profiles (level, years, skills, target_roles, locations, ambiguities, approved_at) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (level, years, skills, target_roles, locations, ambiguities),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            return HTMLResponse(_HEAD + _nav() + f"<h1>Profile</h1><p class='error'>{html.escape(str(e))}</p>" + _TABLE_FOOT)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/rejected", response_class=HTMLResponse)
    def rejected_get(request: Request) -> Response:
        auth = _require_auth(request)
        if auth:
            return auth
        try:
            conn = _open(database)
            rows = conn.execute("SELECT d.*, j.title, j.company, j.url FROM decisions d LEFT JOIN jobs j ON j.id=d.job_id ORDER BY d.id DESC LIMIT 500").fetchall()
            conn.close()
        except Exception as e:
            return HTMLResponse(_HEAD + _nav() + f"<h1>Rejected</h1><p class='error'>{html.escape(str(e))}</p>" + _TABLE_FOOT)
        items = []
        for r in rows:
            decision = str(r["decision"] or "")
            title = html.escape(str(r["title"] or "-"))
            company = html.escape(str(r["company"] or "-"))
            reason = html.escape(str(r["reason"] or "-"))
            items.append(f"<tr><td>{r['job_id']}</td><td>{title}</td><td>{company}</td><td>{decision}</td><td>{reason}</td></tr>")
        content = (
            _HEAD
            + _nav()
            + "<h1>Why not shown</h1><p>Rejected jobs with reasons.</p>"
            + "<table><thead><tr><th>Job ID</th><th>Title</th><th>Company</th><th>Decision</th><th>Reason</th></tr></thead><tbody>"
            + "".join(items)
            + "</tbody></table>"
            + _TABLE_FOOT
        )
        return HTMLResponse(content)

    @app.post("/find")
    def find_jobs(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
        auth = _require_auth(request)
        if auth:
            return auth

        def _run():
            global _pipeline_status
            with _pipeline_lock:
                if _pipeline_status["running"]:
                    return
                _pipeline_status = {
                    "running": True,
                    "stage": "starting",
                    "counts": {"collected": 0, "parsed": 0, "filtered": 0, "evaluated": 0, "verified": 0, "ranked": 0, "decided": 0},
                    "errors": {},
                    "started_at": time.time(),
                    "finished_at": None,
                    "result": None,
                }
            try:
                if run_pipeline is not None:
                    try:
                        run_pipeline(database=database, collect_limit=20)
                    except Exception as e:
                        _pipeline_status["errors"]["pipeline"] = str(e)
                _pipeline_status["stage"] = "done"
            except Exception as e:
                _pipeline_status["errors"]["fatal"] = str(e)
                _pipeline_status["stage"] = "error"
            finally:
                _pipeline_status["running"] = False
                _pipeline_status["finished_at"] = time.time()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return RedirectResponse(url="/status", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status_get(request: Request) -> Response:
        auth = _require_auth(request)
        if auth:
            return auth
        ps = _pipeline_status
        errs = "".join(f"<li>{html.escape(k)}: {html.escape(str(v))}</li>" for k, v in ps["errors"].items()) or "<li>None</li>"
        content = (
            _HEAD
            + _nav()
            + "<h1>Pipeline status</h1>"
            + f"<p>Running: {ps['running']}</p>"
            + f"<p>Stage: {html.escape(str(ps['stage']))}</p>"
            + f"<p>Counts: {html.escape(str(ps['counts']))}</p>"
            + "<p>Errors per source:</p><ul>" + errs + "</ul>"
            + '<form method="post" action="/find"><button type="submit">Find jobs</button></form>'
            + '<script>setTimeout(function(){window.location.reload();}, 2000);</script>'
            + _TABLE_FOOT
        )
        return HTMLResponse(content)

    @app.get("/status.json")
    def status_json(request: Request) -> Response:
        auth = _require_auth(request)
        if auth:
            return auth
        return JSONResponse(_pipeline_status)

    return app
