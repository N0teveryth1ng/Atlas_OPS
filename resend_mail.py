import os
from datetime import datetime
from html import escape
import httpx


def format_application_summary(results: list[dict]) -> str:
    lines = [
        f"Application summary generated: {datetime.utcnow().isoformat()} UTC",
        "",
        f"Total jobs processed: {len(results)}",
        "",
    ]

    for job in results:
        status = job.get("status", "unknown")
        url = job.get("url") or job.get("job_url") or "unknown"
        error = job.get("error")
        line = f"- {status.upper()}: {url}"
        if error:
            line += f" | error: {error}"
        lines.append(line)

    return "\n".join(lines)


def format_application_summary_html(results: list[dict]) -> str:
    groups = [
        ("Submitted applications", {"filled_and_submitted"}, "submitted"),
        ("Successful fills", {"filled_not_submitted"}, "filled, not submitted"),
        ("Submission needs review", {"submitted_unconfirmed"}, "submission clicked; confirmation not detected"),
        ("Skipped jobs", {"skipped"}, "skipped"),
        ("Failed jobs", {"failed"}, "failed"),
    ]
    sections = ["<h2>Job application summary</h2>"]
    for heading, statuses, label in groups:
        jobs = [job for job in results if job.get("status") in statuses]
        if not jobs:
            continue
        items = []
        for job in jobs:
            url = job.get("url") or job.get("job_url") or "unknown"
            error = job.get("error")
            if url.startswith(("https://", "http://")):
                job_label = f'<a href="{escape(url, quote=True)}">{escape(url)}</a>'
            else:
                job_label = escape(url)
            detail = f" — {label}"
            if error:
                detail += f": {escape(error)}"
            items.append(f"<li>{job_label}{detail}</li>")
        sections.append(f"<h3>{heading}</h3><ul>{''.join(items)}</ul>")
    if len(sections) == 1:
        sections.append("<p>No jobs were processed.</p>")
    return "".join(sections)


def email_sender(results: list[dict]) -> bool:
    summary_text = format_application_summary(results)
    summary_html = format_application_summary_html(results)
    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_FROM_EMAIL")
    recipient = os.environ.get("RESEND_TO_EMAIL")

    if not (api_key and sender and recipient):
        print("[resend_mail] Missing Resend configuration, summary not sent.")
        print(summary_text)
        return False

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [recipient],
                "subject": f"Daily Job Application Summary - {datetime.utcnow().strftime('%Y-%m-%d')}",
                "text": summary_text,
                "html": summary_html,
            },
            timeout=30,
        )
        response.raise_for_status()
        print(f"[resend_mail] Summary email sent to {recipient}")
        return True
    except Exception as exc:
        print(f"[resend_mail] Failed to send summary email: {exc}")
        return False
