"""Grounded tailored-resume generator (Phase 8a, PR C1).

Given the approved profile and one parsed job, draft a resume tailored to that
job. The model only ever rewrites the candidate's own material:

* every summary sentence and every bullet must carry a verbatim quote from the
  profile (validated with the same normalisation the evidence validator uses),
* a skill is kept only if the profile already has it (alias-aware, so the job's
  "ReactJS" matches the profile's "React"),
* a line that introduces a number, metric, date, technology or other proper noun
  the profile does not contain is dropped,
* job requirements the profile lacks go to a separate "missing requirements"
  list and are NEVER added to the resume,
* job keywords may appear at most once in Skills and at most once across bullets.

A failing line is dropped; the rest is kept. The structural sections (header,
experience role/company/duration, project titles/tech, education, certifications)
are copied verbatim from the profile, so dates and employers cannot change.

Nothing here submits an application, fetches a URL, or sends profile/resume text
to Braintrust: ``LLMClient.call_json`` traces only prompt fingerprints.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import add_llm_tokens
from .evaluator import job_source_text
from .evidence import quote_valid
from .llm import LLMClient
from .prompt_store import load_prompt
from .render import render_job, render_parsed_jd, render_profile
from .schemas import CandidateProfile, EvidenceSource, Job, Links, ParsedJD
from .skills import SkillOntology, get_ontology

logger = logging.getLogger(__name__)

#: The model is re-prompted at most this many times before the leftover fails are
#: simply dropped.
MAX_RESUME_RETRIES = 2

#: A summary of more than this many sentences is trimmed to this many.
SUMMARY_MAX = 3

#: Headings an ATS expects, in order. A clean layout has all of them.
STANDARD_HEADINGS: tuple[str, ...] = ("Summary", "Skills", "Experience", "Projects", "Education")

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./_-]*")
_STRIP_CHARS = ".,;:!?()[]{}\"'"


# --------------------------------------------------------------------------- #
# Output models
# --------------------------------------------------------------------------- #


class ResumeLine(BaseModel):
    """One sentence/bullet with the verbatim profile quote it rests on."""

    text: str
    quote: str
    source: EvidenceSource = EvidenceSource.profile


class DraftEntry(BaseModel):
    """Bullets the model wrote for one profile experience/project entry."""

    bullets: list[ResumeLine] = Field(default_factory=list)


class ResumeDraft(BaseModel):
    """Exactly what the model must return (lenient; code validates the rest)."""

    summary: list[ResumeLine] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    experience: list[DraftEntry] = Field(default_factory=list)
    projects: list[DraftEntry] = Field(default_factory=list)


class ResumeHeader(BaseModel):
    name: str | None = None
    email: str | None = None
    links: Links = Field(default_factory=Links)


class ExperienceEntry(BaseModel):
    """A profile experience entry copied verbatim, with tailored bullets."""

    role: str | None = None
    company: str | None = None
    duration_months: int | None = None
    bullets: list[ResumeLine] = Field(default_factory=list)


class ProjectEntry(BaseModel):
    """A profile project copied verbatim, with tailored bullets."""

    title: str | None = None
    tech: list[str] = Field(default_factory=list)
    bullets: list[ResumeLine] = Field(default_factory=list)


class ResumeBlockKind(str, Enum):
    heading = "heading"
    paragraph = "paragraph"
    bullet = "bullet"
    table = "table"
    image = "image"
    text_box = "text_box"
    header = "header"
    footer = "footer"
    icon = "icon"


class ResumeBlock(BaseModel):
    """One layout block the ATS linter inspects."""

    kind: ResumeBlockKind
    text: str = ""


#: Block kinds an ATS cannot reliably read.
_FORBIDDEN_BLOCKS: dict[ResumeBlockKind, str] = {
    ResumeBlockKind.table: "tables are not readable by all ATS parsers",
    ResumeBlockKind.image: "images are not readable by ATS parsers",
    ResumeBlockKind.text_box: "text boxes are not readable by ATS parsers",
    ResumeBlockKind.header: "content in a header is not reliably read",
    ResumeBlockKind.footer: "content in a footer is not reliably read",
    ResumeBlockKind.icon: "icon glyphs are not readable as text",
}

#: Zero-width and control characters that hide text from a human reader.
_HIDDEN_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\x00-\x08\x0e-\x1f]")


class ATSReport(BaseModel):
    passed: bool = True
    reasons: list[str] = Field(default_factory=list)


class TailoredResume(BaseModel):
    """A validated, tailored resume ready to store or export."""

    header: ResumeHeader = Field(default_factory=ResumeHeader)
    summary: list[ResumeLine] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    ats: ATSReport = Field(default_factory=ATSReport)
    prompt_version: str = ""
    model: str = ""


# --------------------------------------------------------------------------- #
# Source text
# --------------------------------------------------------------------------- #


def resume_profile_text(profile: CandidateProfile) -> str:
    """The profile text every resume line must quote from.

    Extends :func:`render_profile` with education, certifications and links so a
    line about them has a verbatim source. The evaluator/verifier keep using
    ``render_profile`` unchanged.
    """
    lines = [render_profile(profile)]
    if profile.education:
        education = "; ".join(
            f"{e.degree or '-'}, {e.field or '-'} @ {e.institution or '-'}"
            + (f" (graduated {e.graduation_year})" if e.graduation_year is not None else "")
            for e in profile.education
        )
        lines.append(f"Education: {education}")
    if profile.certifications:
        lines.append("Certifications: " + ", ".join(profile.certifications))
    link_parts = []
    for label in ("github", "linkedin", "portfolio"):
        value = getattr(profile.links, label, None)
        if value:
            link_parts.append(f"{label}: {value}")
    if link_parts:
        lines.append("Links: " + "; ".join(link_parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Deterministic grounding rules
# --------------------------------------------------------------------------- #


def _norm_token(token: str) -> str:
    return token.strip(_STRIP_CHARS).lower()


def _profile_token_set(profile_text: str) -> set[str]:
    return {_norm_token(m.group(0)) for m in _TOKEN_RE.finditer(profile_text)} - {""}


def _tokens(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in _TOKEN_RE.finditer(text)]


def _is_sentence_initial(text: str, start: int) -> bool:
    preceding = text[:start].rstrip()
    return not preceding or preceding[-1] in ".!?\n:;"


def _profile_skill_keys(profile: CandidateProfile, ontology: SkillOntology) -> set[str]:
    keys = set()
    for skill in profile.skills:
        keys.add(skill.canonical_name)
        keys.add(ontology.canonicalize(skill.canonical_name))
        keys.add(ontology.canonicalize(skill.name))
    return keys


def fabricated_tokens(
    text: str,
    *,
    profile_text: str,
    profile_token_set: set[str],
    profile_skill_keys: set[str],
    ontology: SkillOntology,
) -> list[str]:
    """Tokens in ``text`` that assert a fact the profile does not contain.

    Flags a number/metric/date token absent from the profile, a known technology
    that is not one of the profile's skills, and any mid-sentence proper noun
    (capitalised word) absent from the profile. Sentence-initial words and the
    pronoun "I" are ignored so ordinary phrasing is not flagged.
    """
    found: list[str] = []
    for token, start in _tokens(text):
        norm = _norm_token(token)
        if not norm or token == "I":
            continue
        if _is_sentence_initial(text, start):
            continue
        if any(ch.isdigit() for ch in token) and norm not in profile_token_set:
            found.append(token)
            continue
        canonical = ontology.canonicalize(token)
        if canonical in ontology.entries and canonical not in profile_skill_keys:
            found.append(token)
            continue
        if token[0].isupper() and len(token) >= 2 and norm not in profile_token_set:
            found.append(token)
    return sorted(set(found))


def _jd_keywords_used(text: str, jd_canonical: set[str], ontology: SkillOntology) -> set[str]:
    used = set()
    for token, _ in _tokens(text):
        canonical = ontology.canonicalize(token)
        if canonical in jd_canonical:
            used.add(canonical)
    return used


class _Grounder:
    """Applies the deterministic grounding rules to one resume build."""

    def __init__(
        self,
        *,
        profile: CandidateProfile,
        parsed_jd: ParsedJD,
        jd_text: str,
        ontology: SkillOntology,
    ) -> None:
        self.profile_text = resume_profile_text(profile)
        self.profile_tokens = _profile_token_set(self.profile_text)
        self.profile_skill_keys = _profile_skill_keys(profile, ontology)
        self.ontology = ontology
        self.jd_text = jd_text
        self.jd_canonical = {
            ontology.canonicalize(name)
            for name in (*parsed_jd.must_have_skills, *parsed_jd.nice_to_have_skills)
        }
        self.used_keywords: set[str] = set()

    def keep_line(self, line: ResumeLine) -> tuple[bool, str]:
        """Return ``(keep, reason)`` for one summary sentence or bullet."""
        if not line.text.strip():
            return False, "empty text"
        if line.source != EvidenceSource.profile:
            return False, "candidate claim must quote the profile"
        if not quote_valid(line.quote, EvidenceSource.profile, self.jd_text, self.profile_text):
            return False, "quote not found in profile"
        fabricated = fabricated_tokens(
            line.text,
            profile_text=self.profile_text,
            profile_token_set=self.profile_tokens,
            profile_skill_keys=self.profile_skill_keys,
            ontology=self.ontology,
        )
        if fabricated:
            return False, f"ungrounded term(s) not in profile: {', '.join(fabricated)}"
        used = _jd_keywords_used(line.text, self.jd_canonical, self.ontology)
        repeated = used & self.used_keywords
        if repeated:
            return False, f"repeats job keyword(s): {', '.join(sorted(repeated))}"
        self.used_keywords |= used
        return True, ""

    def keep_skills(self, skills: list[str]) -> list[str]:
        """Keep only profile skills, deduped, in the model's relevance order."""
        kept: list[str] = []
        seen: set[str] = set()
        for name in skills:
            canonical = self.ontology.canonicalize(name)
            if canonical not in self.profile_skill_keys or canonical in seen:
                continue
            seen.add(canonical)
            kept.append(name)
        return kept


# --------------------------------------------------------------------------- #
# ATS lint
# --------------------------------------------------------------------------- #


def resume_blocks(resume: TailoredResume) -> list[ResumeBlock]:
    """Flatten a resume into the text blocks an ATS would read."""
    blocks: list[ResumeBlock] = [ResumeBlock(kind=ResumeBlockKind.heading, text="Summary")]
    blocks += [
        ResumeBlock(kind=ResumeBlockKind.paragraph, text=line.text) for line in resume.summary
    ]
    blocks.append(ResumeBlock(kind=ResumeBlockKind.heading, text="Skills"))
    if resume.skills:
        blocks.append(ResumeBlock(kind=ResumeBlockKind.paragraph, text=", ".join(resume.skills)))
    blocks.append(ResumeBlock(kind=ResumeBlockKind.heading, text="Experience"))
    for entry in resume.experience:
        header = f"{entry.role or '-'} @ {entry.company or '-'}"
        if entry.duration_months is not None:
            header += f" ({entry.duration_months} months)"
        blocks.append(ResumeBlock(kind=ResumeBlockKind.paragraph, text=header))
        blocks += [ResumeBlock(kind=ResumeBlockKind.bullet, text=b.text) for b in entry.bullets]
    blocks.append(ResumeBlock(kind=ResumeBlockKind.heading, text="Projects"))
    for project in resume.projects:
        title = project.title or "-"
        if project.tech:
            title += f" [{', '.join(project.tech)}]"
        blocks.append(ResumeBlock(kind=ResumeBlockKind.paragraph, text=title))
        blocks += [ResumeBlock(kind=ResumeBlockKind.bullet, text=b.text) for b in project.bullets]
    blocks.append(ResumeBlock(kind=ResumeBlockKind.heading, text="Education"))
    for item in resume.education:
        parts = [
            str(item.get(key))
            for key in ("degree", "field", "institution", "graduation_year")
            if item.get(key) not in (None, "")
        ]
        blocks.append(ResumeBlock(kind=ResumeBlockKind.paragraph, text=", ".join(parts)))
    if resume.certifications:
        blocks.append(ResumeBlock(kind=ResumeBlockKind.heading, text="Certifications"))
        blocks.append(
            ResumeBlock(kind=ResumeBlockKind.paragraph, text=", ".join(resume.certifications))
        )
    return blocks


def ats_lint(target: TailoredResume | list[ResumeBlock]) -> ATSReport:
    """Check a resume's layout against basic ATS expectations.

    Fails on any table, image, text box, icon or header/footer content, on hidden
    text, on empty text blocks, and on a missing standard heading. Pure text in a
    single column with all standard headings passes.
    """
    blocks = resume_blocks(target) if isinstance(target, TailoredResume) else target
    reasons: list[str] = []
    for block in blocks:
        message = _FORBIDDEN_BLOCKS.get(block.kind)
        if message:
            reasons.append(f"{block.kind.value}: {message}")
        if _HIDDEN_RE.search(block.text):
            reasons.append(f"{block.kind.value}: hidden characters in text")
        if (
            block.kind
            in (
                ResumeBlockKind.paragraph,
                ResumeBlockKind.bullet,
                ResumeBlockKind.heading,
            )
            and not block.text.strip()
        ):
            reasons.append(f"{block.kind.value}: empty text")
    headings = {b.text for b in blocks if b.kind == ResumeBlockKind.heading}
    for heading in STANDARD_HEADINGS:
        if heading not in headings:
            reasons.append(f"missing standard heading: {heading}")
    return ATSReport(passed=not reasons, reasons=reasons)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def build_user_prompt(profile: CandidateProfile, job: Job, parsed_jd: ParsedJD) -> str:
    return (
        "## Candidate profile\n"
        f"{resume_profile_text(profile)}\n\n"
        "## Job requirements\n"
        f"{render_parsed_jd(parsed_jd)}\n\n"
        "## Raw job posting\n"
        f"{render_job(job)}"
    )


def _retry_message(errors: list[str]) -> str:
    return (
        "Your previous draft did not pass validation:\n"
        + "\n".join(f"- {error}" for error in errors)
        + "\nFix every item. Use ONLY facts in the candidate profile; every summary "
        "sentence and bullet needs a verbatim profile quote; introduce no number, "
        "metric, date, technology or proper noun the profile lacks; list each job "
        "keyword at most once."
    )


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _keep_lines(lines: list[ResumeLine], grounder: _Grounder, label: str) -> list[ResumeLine]:
    kept: list[ResumeLine] = []
    for line in lines:
        keep, reason = grounder.keep_line(line)
        if keep:
            kept.append(ResumeLine(text=line.text, quote=line.quote, source=line.source))
        else:
            logger.warning("tailored-resume %s dropped: %s", label, reason)
    return kept


def _draft_errors(draft: ResumeDraft) -> list[str]:
    errors: list[str] = []
    if len(draft.summary) > SUMMARY_MAX:
        errors.append(f"summary must have at most {SUMMARY_MAX} sentences")
    return errors


def _assemble(
    draft: ResumeDraft,
    *,
    profile: CandidateProfile,
    parsed_jd: ParsedJD,
    version: str,
    model: str,
    jd_text: str,
    ontology: SkillOntology,
) -> TailoredResume:
    grounder = _Grounder(profile=profile, parsed_jd=parsed_jd, jd_text=jd_text, ontology=ontology)

    summary = _keep_lines(draft.summary, grounder, "summary")[:SUMMARY_MAX]
    skills = grounder.keep_skills(draft.skills)

    profile_experience = [*profile.internships, *profile.work]
    experience: list[ExperienceEntry] = []
    for index, entry in enumerate(profile_experience):
        bullets = draft.experience[index].bullets if index < len(draft.experience) else []
        experience.append(
            ExperienceEntry(
                role=entry.role,
                company=entry.company,
                duration_months=entry.duration_months,
                bullets=_keep_lines(bullets, grounder, f"experience[{index}]"),
            )
        )

    projects: list[ProjectEntry] = []
    for index, project in enumerate(profile.projects):
        bullets = draft.projects[index].bullets if index < len(draft.projects) else []
        projects.append(
            ProjectEntry(
                title=project.title,
                tech=list(project.tech),
                bullets=_keep_lines(bullets, grounder, f"projects[{index}]"),
            )
        )

    missing = _missing_requirements(profile, parsed_jd, ontology)
    resume = TailoredResume(
        header=ResumeHeader(
            name=profile.full_name,
            email=profile.email,
            links=profile.links,
        ),
        summary=summary,
        skills=skills,
        experience=experience,
        projects=projects,
        education=[edu.model_dump() for edu in profile.education],
        certifications=list(profile.certifications),
        missing_requirements=missing,
        prompt_version=version,
        model=model,
    )
    resume.ats = ats_lint(resume)
    return resume


def _missing_requirements(
    profile: CandidateProfile, parsed_jd: ParsedJD, ontology: SkillOntology
) -> list[str]:
    """Job requirements the profile lacks; shown to the owner, never added."""
    have = _profile_skill_keys(profile, ontology)
    missing: list[str] = []
    for name in (*parsed_jd.must_have_skills, *parsed_jd.nice_to_have_skills):
        if ontology.canonicalize(name) not in have and name not in missing:
            missing.append(name)
    return missing


def generate_tailored_resume(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
) -> TailoredResume:
    """Draft and validate a tailored resume for ``job`` from ``profile``."""
    settings = settings or get_settings()
    ontology = ontology or get_ontology()
    system_prompt, version = load_prompt("tailored_resume")
    base_user = build_user_prompt(profile, job, parsed_jd)
    jd_text = job_source_text(job)
    model = settings.models.evaluator

    logger.info("Tailored resume: drafting for '%s' @ '%s'", job.title, job.company)
    draft: ResumeDraft | None = None
    errors: list[str] = []
    for attempt in range(MAX_RESUME_RETRIES + 1):
        user = base_user if attempt == 0 else f"{base_user}\n\n{_retry_message(errors)}"
        draft = client.call_json(
            schema=ResumeDraft,
            system=system_prompt,
            user=user,
            model=model,
        )
        errors = _draft_errors(draft)
        if not errors:
            break
        logger.warning(
            "tailored-resume draft failed validation (attempt %s/%s): %s",
            attempt + 1,
            MAX_RESUME_RETRIES + 1,
            errors,
        )
    return _assemble(
        cast(ResumeDraft, draft),
        profile=profile,
        parsed_jd=parsed_jd,
        version=version,
        model=model,
        jd_text=jd_text,
        ontology=ontology,
    )


def generate_tailored_resume_under_caps(
    conn: Any,
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    settings: Settings | None = None,
) -> TailoredResume:
    """Generate a tailored resume under the per-run caps and daily-limit guard.

    Wraps the client so ``max_llm_calls_per_run`` and ``max_llm_tokens_per_run``
    are enforced, records the tokens spent against today's Groq budget, and lets
    :class:`~atlas.llm.GroqDailyLimitReached` propagate to the caller.
    """
    from .pipeline import _BudgetedClient

    settings = settings or get_settings()
    start_tokens = int(getattr(client, "tokens_used", 0) or 0)
    budgeted = cast(
        LLMClient,
        _BudgetedClient(
            client,
            settings.run.max_llm_calls_per_run,
            settings.run.max_llm_tokens_per_run,
        ),
    )
    try:
        return generate_tailored_resume(
            budgeted, profile=profile, job=job, parsed_jd=parsed_jd, settings=settings
        )
    finally:
        delta = int(getattr(budgeted, "tokens_used", 0) or 0) - start_tokens
        if delta > 0:
            add_llm_tokens(conn, delta)
