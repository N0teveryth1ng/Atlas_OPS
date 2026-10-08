"""Local dashboard for matched jobs and their application status.

Served by ``python -m atlas.cli dashboard`` (or ``uvicorn app:app``). One
server-rendered HTML page plus a JSON view — no JavaScript framework, no
template engine, no outbound requests.

``APLD`` / ``NTAPLD`` / ``PEND`` are manual bookkeeping: the dashboard only
stores what a human clicked, it never applies to a job anywhere.
"""

from __future__ import annotations

import html
import sqlite3
from pathlib import Path

from fastapi import FastAPI
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
<title>Atlas — matched jobs</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1f2328; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #d0d7de; padding: 0.45rem 0.6rem; text-align: left; }
th { background: #f6f8fa; }
a { color: #0969da; }
.badge { color: #fff; border: 0; border-radius: 999px; padding: 0.15rem 0.6rem;
         font-size: 0.8rem; display: inline-block; }
.actions form { display: inline-block; margin-right: 0.25rem; }
button.badge { cursor: pointer; opacity: 0.5; }
button.badge:hover { opacity: 1; }
.notice { background: #fff8c5; border: 1px solid #d4a72c; padding: 0.75rem; }
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


def create_app(database: Path | None = None) -> FastAPI:
    """Build the dashboard ASGI app (``database`` defaults to ``ATLAS_DB``)."""
    app = FastAPI(title="Atlas dashboard", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        try:
            conn = _open(database)
        except sqlite3.Error as exc:
            return HTMLResponse(_page([], notice=f"Database unavailable: {exc}"))
        try:
            rows = matched_jobs(conn)
        finally:
            conn.close()
        return HTMLResponse(_page(rows))

    @app.post("/jobs/{job_id}/status/{status}")
    def update_status(job_id: int, status: str) -> Response:
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
    def api_matched() -> Response:
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
