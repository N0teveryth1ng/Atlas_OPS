"""Digest builder (plan section 5.11).

One digest per run. Groups ranked jobs into sections and renders both a compact
HTML email and a plain-text fallback. Also carries the per-run funnel summary
(fetched -> deduped -> filtered -> evaluated -> sent) that drives tuning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .config import Settings
from .job_status import SHIPPABLE, InvariantViolation, JobStatus
from .normalize import source_for_url
from .schemas import Recommendation

MAX_REASONS = 3


class SourceCredit(BaseModel):
    """One provider that contributed a URL to a job, with its own direct link.

    Attribution is derived from already-persisted URLs (``job.urls``), so no
    schema or DB column is needed: terms like Himalayas' link-back and Jobicy's
    friendlyNotice are satisfied even when dedupe made another source's URL the
    canonical ``job.url``.
    """

    name: str
    url: str


class DigestItem(BaseModel):
    job_id: int | None = None
    title: str = ""
    company: str = ""
    location: str = ""
    urls: list[str] = Field(default_factory=list)
    credits: list[SourceCredit] = Field(default_factory=list)
    score: float = 0.0
    recommendation: str = Recommendation.maybe.value
    reasons_for: list[str] = Field(default_factory=list)
    main_risk: str | None = None
    missing_skills: list[str] = Field(default_factory=list)
    seniority_assessment: str | None = None
    needs_review: bool = False
    status: str = JobStatus.new.value
    filter_passed: bool = False


class DigestSection(BaseModel):
    title: str
    items: list[DigestItem] = Field(default_factory=list)


class RunSummary(BaseModel):
    processed: int = 0
    filtered_out: int = 0
    rejection_counts: dict[str, int] = Field(default_factory=dict)
    evaluated: int = 0
    sent: int = 0
    quote_validation_failures: int = 0


class Digest(BaseModel):
    run_id: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    subject: str = ""
    summary: RunSummary = Field(default_factory=RunSummary)
    sections: list[DigestSection] = Field(default_factory=list)

    @property
    def item_count(self) -> int:
        return sum(len(section.items) for section in self.sections)

    def job_ids(self) -> list[int]:
        return [item.job_id for section in self.sections for item in section.items if item.job_id]

    def is_empty(self) -> bool:
        return self.item_count == 0


def _job_urls(job) -> list[str]:
    seen: list[str] = []
    for url in (job.url, *job.urls):
        if url and url not in seen:
            seen.append(url)
    return seen


def _source_credits(urls: list[str]) -> list[SourceCredit]:
    """One credit per contributing source, order preserved from ``urls``.

    Known sources deduplicate by name (first URL wins); an unknown host is kept
    per URL under its hostname so no URL is ever lost from the digest.
    """
    credits: list[SourceCredit] = []
    seen_sources: set[str] = set()
    seen_unknown: set[str] = set()
    for url in urls:
        source = source_for_url(url)
        if source is not None:
            if source in seen_sources:
                continue
            seen_sources.add(source)
            name = source
        else:
            if url in seen_unknown:
                continue
            seen_unknown.add(url)
            name = urlparse(url).hostname or "link"
        credits.append(SourceCredit(name=name, url=url))
    return credits


def _item_from_result(result) -> DigestItem:
    verdict = result.verdict
    verifier = result.verifier
    match = result.skill_match

    risks: list[str] = []
    if verdict is not None:
        risks.extend(item.quote for item in verdict.reasons_against)
    if verifier is not None and verifier.veto:
        risks.extend(item.quote for item in verifier.reasons_against)
    reasons_for = [item.quote for item in verdict.reasons_for] if verdict is not None else []

    urls = _job_urls(result.job)
    return DigestItem(
        job_id=result.job_id,
        title=result.job.title or "",
        company=result.job.company or "",
        location=result.job.location or "",
        urls=urls,
        credits=_source_credits(urls),
        score=result.score,
        recommendation=result.final_recommendation.value,
        reasons_for=reasons_for[:MAX_REASONS],
        main_risk=risks[0] if risks else None,
        missing_skills=list(match.missing_must_haves) if match is not None else [],
        seniority_assessment=(
            verdict.seniority_assessment.quote
            if verdict is not None and verdict.seniority_assessment
            else None
        ),
        needs_review=result.needs_review,
        status=result.status.value,
        filter_passed=bool(result.filter_result is not None and result.filter_result.passed),
    )


def build_digest(
    results: list,
    settings: Settings,
    *,
    run_id: int | None = None,
    already_sent: set[int] | None = None,
    subject: str | None = None,
) -> Digest:
    already_sent = already_sent or set()
    summary = RunSummary(processed=len(results))

    strong: list[DigestItem] = []
    worth: list[DigestItem] = []

    for result in results:
        # Invariant (audit D-4/L8): a needs_review job must never be rendered.
        if result.status == JobStatus.needs_review:
            raise InvariantViolation("needs_review job reached the digest builder")
        if result.filter_result is None:
            # A missing filter result row is treated as not passed.
            summary.filtered_out += 1
            continue
        if not result.filter_result.passed:
            summary.filtered_out += 1
            for rejection in result.filter_result.rejections:
                summary.rejection_counts[rejection.rule_id] = (
                    summary.rejection_counts.get(rejection.rule_id, 0) + 1
                )
            continue
        if result.verdict is not None:
            summary.evaluated += 1
        if result.job_id in already_sent:
            continue
        if result.final_recommendation == Recommendation.skip:
            continue

        # This result is about to be rendered: enforce the shipping invariant.
        if result.status not in SHIPPABLE or result.verdict is None:
            raise InvariantViolation(
                f"unverified job reached the digest: status={result.status.value}"
            )

        item = _item_from_result(result)
        if result.final_recommendation == Recommendation.strong_apply:
            strong.append(item)
        else:
            worth.append(item)

    for bucket in (strong, worth):
        bucket.sort(key=lambda item: item.score, reverse=True)

    sections: list[DigestSection] = []
    if strong:
        sections.append(DigestSection(title="Strong matches", items=strong))
    if worth:
        sections.append(DigestSection(title="Worth a look", items=worth))

    summary.sent = sum(len(section.items) for section in sections)
    digest = Digest(run_id=run_id, summary=summary, sections=sections)
    digest.subject = subject or (
        f"Atlas: {summary.sent} job match{'es' if summary.sent != 1 else ''}"
    )
    return digest


def _summary_line(summary: RunSummary) -> str:
    parts = [
        f"processed {summary.processed}",
        f"filtered out {summary.filtered_out}",
        f"evaluated {summary.evaluated}",
        f"sent {summary.sent}",
    ]
    return " / ".join(parts)


def render_text(digest: Digest) -> str:
    lines = [digest.subject, "=" * len(digest.subject), ""]
    lines.append(_summary_line(digest.summary))
    if digest.summary.rejection_counts:
        counts = ", ".join(
            f"{rule}:{count}" for rule, count in sorted(digest.summary.rejection_counts.items())
        )
        lines.append(f"rejections: {counts}")
    lines.append("")

    if digest.is_empty():
        lines.append("No matches met the bar today. Sending nothing is a valid result.")
        return "\n".join(lines)

    for section in digest.sections:
        lines.append(f"## {section.title}")
        for item in section.items:
            lines.append(f"- [{item.score:.0f}] {item.title} @ {item.company} ({item.location})")
            if item.urls:
                lines.append(f"    {item.urls[0]}")
            for credit in item.credits:
                lines.append(f"    via {credit.name}: {credit.url}")
            lines.append(f"    fit: {item.recommendation}")
            for reason in item.reasons_for:
                lines.append(f"    + {reason}")
            if item.main_risk:
                lines.append(f"    ! risk: {item.main_risk}")
            if item.missing_skills:
                lines.append(f"    - missing: {', '.join(item.missing_skills)}")
            if item.seniority_assessment:
                lines.append(f"    seniority: {item.seniority_assessment}")
        lines.append("")
    return "\n".join(lines)


def render_html(digest: Digest) -> str:
    def esc(value: object) -> str:
        return escape(str(value or ""))

    rows: list[str] = []
    rows.append(
        "<div style='font-family:system-ui,Segoe UI,sans-serif;max-width:680px;margin:auto;"
        "color:#1a1a1a'>"
    )
    rows.append(f"<h2 style='margin:0 0 4px'>{esc(digest.subject)}</h2>")
    rows.append(f"<p style='color:#666;margin:0 0 16px'>{esc(_summary_line(digest.summary))}</p>")
    if digest.summary.rejection_counts:
        counts = ", ".join(
            f"{esc(rule)}: {count}"
            for rule, count in sorted(digest.summary.rejection_counts.items())
        )
        rows.append(
            f"<p style='color:#999;font-size:12px;margin:0 0 16px'>rejections: {counts}</p>"
        )

    if digest.is_empty():
        rows.append("<p>No matches met the bar today. Sending nothing is a valid result.</p>")

    for section in digest.sections:
        rows.append(
            f"<h3 style='margin:20px 0 8px;border-bottom:1px solid #eee;padding-bottom:4px'>"
            f"{esc(section.title)}</h3>"
        )
        for item in section.items:
            link = item.urls[0] if item.urls else ""
            title = esc(item.title or "Untitled role")
            title_html = f"<a href='{esc(link)}' style='color:#0b5'>{title}</a>" if link else title
            rows.append(
                "<div style='border:1px solid #eee;border-radius:8px;padding:12px;margin:10px 0'>"
            )
            rows.append(
                f"<div style='display:flex;justify-content:space-between'>"
                f"<strong>{title_html}</strong>"
                f"<span style='color:#0b5;font-weight:600'>{item.score:.0f}</span></div>"
            )
            rows.append(
                f"<div style='color:#555;font-size:13px'>{esc(item.company)} &middot; "
                f"{esc(item.location)} &middot; {esc(item.recommendation)}</div>"
            )
            if item.credits:
                links = ", ".join(
                    f"<a href='{esc(credit.url)}' style='color:#0b5'>{esc(credit.name)}</a>"
                    for credit in item.credits
                )
                rows.append(
                    f"<div style='color:#666;font-size:12px;margin:3px 0 0'>Via {links}</div>"
                )
            if item.reasons_for:
                rows.append("<ul style='margin:8px 0;padding-left:18px'>")
                for reason in item.reasons_for:
                    rows.append(f"<li>{esc(reason)}</li>")
                rows.append("</ul>")
            if item.main_risk:
                rows.append(f"<p style='color:#b00;margin:4px 0'>Risk: {esc(item.main_risk)}</p>")
            if item.missing_skills:
                rows.append(
                    f"<p style='color:#a60;margin:4px 0'>Missing: {esc(', '.join(item.missing_skills))}</p>"
                )
            if item.seniority_assessment:
                rows.append(
                    f"<p style='color:#666;margin:4px 0;font-size:13px'>"
                    f"Seniority: {esc(item.seniority_assessment)}</p>"
                )
            rows.append("</div>")
    rows.append("</div>")
    return "\n".join(rows)
