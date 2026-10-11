"""Grounded application kit: summary, cover note and Q&A for one job.

Phase 8a. Every answer in the kit must trace to a verbatim quote from the
approved profile or the job posting, validated with the same normalisation the
evidence validator applies to evaluator verdicts. Answers whose quote does not
validate are dropped; the dashboard shows them as "needs your input". Nothing in
this module submits an application or drives a browser, and no kit content is
sent to Braintrust (``LLMClient.call_json`` only traces prompt fingerprints).
"""

from __future__ import annotations

import logging
import re
from typing import cast

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .evaluator import job_source_text
from .evidence import quote_valid
from .llm import LLMClient
from .prompt_store import load_prompt
from .render import render_job, render_profile
from .schemas import CandidateProfile, EvidenceSource, Job

logger = logging.getLogger(__name__)

#: Bounds are only an upper bound on corrections: a draft is re-prompted at most
#: this many times before the leftover parts are dropped.
MAX_KIT_RETRIES = 2

#: The common questions every kit answers, in render order. Answers missing or
#: unsupported by a verbatim quote are omitted and surfaced as "needs your
#: input" by the dashboard.
KIT_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("why_company", "Why this company?"),
    ("relevant_experience", "Relevant experience for this role"),
    ("availability", "Notice period / availability"),
    ("links", "Links (portfolio, GitHub, LinkedIn)"),
)

_QUESTION_LABELS = dict(KIT_QUESTIONS)
_QUESTION_KEYS = set(_QUESTION_LABELS)

_SENTENCE_SPLIT = re.compile(r"[.!?]+(?:\s+|$)")


class KitAnswer(BaseModel):
    """One grounded answer as produced by the model."""

    question_key: str
    answer: str
    quote: str
    source: EvidenceSource


class KitDraft(BaseModel):
    """Layout the model must return for a kit."""

    summary: str = ""
    cover_note: str = ""
    answers: list[KitAnswer] = Field(default_factory=list)


class KitSection(BaseModel):
    """Free-text section; ``blocked_reason`` set when the model never produced
    a section that met the deterministic bounds."""

    text: str = ""
    blocked_reason: str | None = None


class KitAnswerResult(BaseModel):
    """A validated, keepable answer."""

    question_key: str
    question: str
    answer: str
    quote: str
    source: EvidenceSource


class ApplicationKit(BaseModel):
    """Validated kit ready to store/display."""

    summary: KitSection = Field(default_factory=KitSection)
    cover_note: KitSection = Field(default_factory=KitSection)
    answers: list[KitAnswerResult] = Field(default_factory=list)
    prompt_version: str = ""
    model: str = ""


# --------------------------------------------------------------------------- #
# Deterministic text checks
# --------------------------------------------------------------------------- #


def sentence_count(text: str) -> int:
    """Number of sentences in ``text``, splitting on terminal punctuation."""
    return len([part for part in _SENTENCE_SPLIT.split(text.strip()) if part.strip()])


def word_count(text: str) -> int:
    return len(text.split())


def kit_profile_text(profile: CandidateProfile) -> str:
    """The 'profile' text a kit's answers must quote from.

    ``render_profile`` covers the main facts; education, locations,
    certifications and links are appended as canonical lines so answers about
    them have a verbatim source. Kit-only text: the evaluator and verifier keep
    using ``render_profile`` unchanged.
    """
    lines = [render_profile(profile)]
    if profile.education:
        education = "; ".join(
            f"{e.degree or '-'}, {e.field or '-'} @ {e.institution or '-'}"
            + (f" (graduated {e.graduation_year})" if e.graduation_year is not None else "")
            for e in profile.education
        )
        lines.append(f"Education: {education}")
    if profile.locations:
        lines.append(f"Locations: {', '.join(profile.locations) or '-'}")
    if profile.certifications:
        lines.append(f"Certifications: {', '.join(profile.certifications)}")
    link_parts = []
    for label in ("github", "linkedin", "portfolio"):
        url = getattr(profile.links, label, None)
        if url:
            link_parts.append(f"{label}: {url}")
    if link_parts:
        lines.append("Links: " + "; ".join(link_parts))
    return "\n".join(lines)


def build_user_prompt(profile: CandidateProfile, job: Job) -> str:
    question_lines = "\n".join(f"- {key}: {label}" for key, label in KIT_QUESTIONS)
    return (
        "## Candidate profile\n"
        f"{kit_profile_text(profile)}\n\n"
        "## Raw job posting\n"
        f"{render_job(job)}\n\n"
        "## Questions to answer\n"
        f"{question_lines}"
    )


def _retry_message(errors: list[str]) -> str:
    return (
        "Your previous draft did not pass validation:\n"
        + "\n".join(f"- {error}" for error in errors)
        + "\nFix every item. Omit any question you cannot answer with a verbatim "
        "quote; keep summary to exactly 3 sentences and cover_note under 150 words."
    )


def _validate_draft(draft: KitDraft, jd_text: str, profile_text: str) -> list[str]:
    errors: list[str] = []
    summary_sentences = sentence_count(draft.summary)
    if summary_sentences != 3:
        errors.append(f"summary must be exactly 3 sentences (found {summary_sentences})")
    cover_words = word_count(draft.cover_note)
    if cover_words >= 150:
        errors.append(f"cover_note must be under 150 words (found {cover_words})")
    for index, answer in enumerate(draft.answers):
        if answer.question_key not in _QUESTION_KEYS:
            errors.append(f"answers[{index}] unknown question_key: {answer.question_key!r}")
            continue
        if not answer.answer.strip():
            errors.append(f"answers[{index}] empty answer")
            continue
        if not quote_valid(answer.quote, answer.source, jd_text, profile_text):
            errors.append(
                f"answers[{index}] quote not found in {answer.source.value}: {answer.quote!r}"
            )
    return errors


def _assemble(
    draft: KitDraft,
    *,
    version: str,
    model: str,
    jd_text: str,
    profile_text: str,
) -> ApplicationKit:
    """Keep only the parts that meet the deterministic bounds and the evidence
    rules; everything else is dropped so the dashboard can flag it as
    "needs your input"."""
    if sentence_count(draft.summary) == 3:
        summary = KitSection(text=draft.summary)
    else:
        summary = KitSection(blocked_reason="needs your input")
    if word_count(draft.cover_note) < 150:
        cover_note = KitSection(text=draft.cover_note)
    else:
        cover_note = KitSection(blocked_reason="needs your input")

    answers: list[KitAnswerResult] = []
    for index, answer in enumerate(draft.answers):
        if answer.question_key not in _QUESTION_KEYS or not answer.answer.strip():
            continue
        if not quote_valid(answer.quote, answer.source, jd_text, profile_text):
            logger.warning(
                "application-kit answer %r dropped: quote not found in %s",
                answer.question_key,
                answer.source.value,
            )
            continue
        answers.append(
            KitAnswerResult(
                question_key=answer.question_key,
                question=_QUESTION_LABELS[answer.question_key],
                answer=answer.answer,
                quote=answer.quote,
                source=answer.source,
            )
        )
    return ApplicationKit(
        summary=summary,
        cover_note=cover_note,
        answers=answers,
        prompt_version=version,
        model=model,
    )


def generate_kit(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    settings: Settings | None = None,
) -> ApplicationKit:
    """Draft and validate an application kit for ``job`` from ``profile``.

    Uses only the approved profile and the raw job posting. Quotes are checked
    against the same texts the calibrated evidence validator uses
    (:func:`job_source_text` for the job, :func:`kit_profile_text` for the
    profile); answers that cannot prove a verbatim quote are dropped.
    """
    settings = settings or get_settings()
    system_prompt, version = load_prompt("application_kit")
    base_user = build_user_prompt(profile, job)
    jd_text = job_source_text(job)
    profile_text = kit_profile_text(profile)
    model = settings.models.evaluator

    logger.info("Application kit: drafting for '%s' @ '%s'", job.title, job.company)
    draft: KitDraft | None = None
    errors: list[str] = []
    for attempt in range(MAX_KIT_RETRIES + 1):
        user = base_user if attempt == 0 else f"{base_user}\n\n{_retry_message(errors)}"
        draft = client.call_json(
            schema=KitDraft,
            system=system_prompt,
            user=user,
            model=model,
        )
        errors = _validate_draft(draft, jd_text, profile_text)
        if not errors:
            break
        logger.warning(
            "application-kit draft failed validation (attempt %s/%s): %s",
            attempt + 1,
            MAX_KIT_RETRIES + 1,
            errors,
        )
    kit = _assemble(
        cast(KitDraft, draft),
        version=version,
        model=model,
        jd_text=jd_text,
        profile_text=profile_text,
    )
    if errors:
        logger.warning(
            "application kit finished with validation errors; dropped parts will "
            "be shown as 'needs your input': %s",
            errors,
        )
    return kit
