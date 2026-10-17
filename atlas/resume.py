"""Grounded tailored-resume generator (Phase 8a, PRs C1/C1b).

Given the approved profile and one parsed job, draft a resume tailored to that job.
Grounding is by *fact IDs*, not free-text quotes:

* the profile is rendered to the model as numbered facts with stable ids
  (``[P1.summary]``, ``[I1.description]``, ``[ED1.line]``, ...);
* every summary sentence and bullet must cite one or more of those ids;
* code maps each id back to its verbatim text and verifies that every technology,
  proper noun and number in the line appears in the cited text (or that entry's tech
  list), so a line cannot add a claim the cited facts do not contain;
* skills are assembled in code, never by the model: JD-matching profile skills first,
  then the other non-familiar skills, with "familiar" skills in a separate group;
* a line is rejected when it is third-person / meta, when a bullet does not start
  with an action verb, when it carries hidden characters, when it adds a technology,
  number or proper noun its cited facts lack, when it claims ownership / leadership /
  reliability / scale the facts do not state, or when any JD keyword would exceed the
  global cap; generic nouns (data, design, system, API, AI, service, platform, pipeline)
  never need grounding;
* skills are evidence-linked: a skill is only in the main group when a project or
  internship/work tech list, or a project summary/outcomes, or an internship/work
  description, contains it (or the owner confirmed it), otherwise it moves to
  "Familiar with (confirm before use)";
* job requirements are classified covered / partial / missing with the fact id and
  quote that support them; a requirement with a concept family is judged only by that
  family, so "data structures" is missing unless a fact names a data structure or
  algorithm.

A failing line is dropped (with its reason). Structural fields (header, role/company/
duration, project titles/tech, education, certifications) are copied verbatim from the
profile. If the profile lacks internship/work/project/link detail the generator
refuses and lists exactly what to add on the Profile page, and never pads.

Nothing here submits an application, fetches a URL, or sends profile/resume text to
Braintrust: ``LLMClient.call_json`` traces only prompt fingerprints.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import add_llm_tokens
from .evidence import quote_valid
from .llm import LLMClient
from .prompt_store import load_prompt
from .render import render_job, render_parsed_jd
from .schemas import CandidateProfile, EvidenceSource, Job, ParsedJD, Proficiency
from .skills import SkillOntology, get_ontology

logger = logging.getLogger(__name__)

#: The model is re-prompted at most this many times before the leftover fails are
#: simply dropped.
MAX_RESUME_RETRIES = 2

#: A summary of more than this many sentences is trimmed to this many.
SUMMARY_MAX = 3

#: A job keyword may appear at most this many times across the whole resume; the
#: Skills section counts once toward each keyword it lists.
KEYWORD_MAX = 3

#: Headings an ATS expects, in order. A clean layout has all of them.
STANDARD_HEADINGS: tuple[str, ...] = ("Summary", "Skills", "Experience", "Projects", "Education")

#: Heading for the second, lower-confidence skill group.
FAMILIAR_HEADING = "Familiar with (confirm before use)"

#: Words that claim ownership, leadership, reliability or scale. Each must appear
#: verbatim in the cited fact text, so a "Sole engineer" fact cannot license "led"
#: or "architected", and a plain project cannot license "production-grade",
#: "from scratch", "core", "enterprise", "scalable", "end-to-end" or "delivered".
_CLAIM_TERMS: tuple[str, ...] = (
    "led",
    "lead",
    "leads",
    "leading",
    "owned",
    "owning",
    "own",
    "owns",
    "architected",
    "architect",
    "production-grade",
    "from scratch",
    "core",
    "enterprise",
    "scalable",
    "end-to-end",
    "delivered",
    "zero-downtime",
    "robust",
    "reliable",
    "high-scale",
    "millions",
    "expert",
    "mentored",
    "mentor",
    "managed",
    "managing",
    "manage",
)

#: Generic nouns that never need grounding (they carry no technology or number).
_GENERIC_TERMS: frozenset[str] = frozenset(
    {
        "data",
        "design",
        "system",
        "systems",
        "api",
        "apis",
        "ai",
        "service",
        "services",
        "platform",
        "platforms",
        "pipeline",
        "pipelines",
        "llm",
        "llms",
        "app",
        "apps",
        "application",
        "applications",
        "tool",
        "tools",
        "workflow",
        "workflows",
        "module",
        "modules",
    }
)

#: Synonyms applied on both sides of a grounding comparison.
_SYNONYMS: dict[str, str] = {"ai": "llm", "api": "rest", "apis": "rest"}

#: Data structures and algorithms a "data structures" requirement may lean on.
_DATA_STRUCTURE_TERMS: tuple[str, ...] = (
    "array",
    "arrays",
    "hashmap",
    "hashtable",
    "dictionary",
    "tree",
    "trees",
    "graph",
    "graphs",
    "queue",
    "queues",
    "stack",
    "stacks",
    "heap",
    "linkedlist",
    "trie",
    "sorting",
    "binarysearch",
    "recursion",
    "dynamicprogramming",
    "algorithm",
    "algorithms",
)

#: How a job requirement is covered by a profile concept family. Keys are
#: key-normalised requirement wordings; values are words that must be *named* in a
#: project/internship/work fact (its tech list or its summary/outcomes/description
#: prose) to satisfy the requirement. A family is deliberately narrow: "PostgreSQL"
#: satisfies "storage", not "system design patterns"; a caching/queue fact satisfies
#: "data structures" only if it actually names a data structure or algorithm.
_REQUIREMENT_FAMILIES: dict[str, tuple[str, ...]] = {
    "api": ("fastapi", "flask", "django", "graphql", "rest"),
    "apis": ("fastapi", "flask", "django", "graphql", "rest"),
    "restapi": ("fastapi", "flask", "django", "graphql", "rest"),
    "restapis": ("fastapi", "flask", "django", "graphql", "rest"),
    "softwaredesignpatterns": ("idempotency", "circuitbreakers", "caching"),
    "softwarearchitecturepatterns": ("idempotency", "circuitbreakers", "caching"),
    "designpatterns": ("idempotency", "circuitbreakers", "caching"),
    "systemdesignpatterns": ("idempotency", "circuitbreakers", "caching"),
    "datastructures": _DATA_STRUCTURE_TERMS,
    "datastructure": _DATA_STRUCTURE_TERMS,
    "algorithms": _DATA_STRUCTURE_TERMS,
    "algorithm": _DATA_STRUCTURE_TERMS,
    "storagesolutions": ("postgresql", "redis", "mongodb", "mysql", "sql", "sqlite", "dynamodb"),
    "storage": ("postgresql", "redis", "mongodb", "mysql", "sql", "sqlite", "dynamodb"),
    "databases": ("postgresql", "redis", "mongodb", "mysql", "sql", "sqlite", "dynamodb"),
    "database": ("postgresql", "redis", "mongodb", "mysql", "sql", "sqlite", "dynamodb"),
    "cloudinfrastructure": ("aws", "gcp", "azure"),
    "cloud": ("aws", "gcp", "azure"),
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

#: A trailing "(strong)"/"(working)"/"(familiar)" tag some profiles carry.
_PROFICIENCY_SUFFIX_RE = re.compile(r"\s*\((?:strong|working|familiar)\)\s*$", re.IGNORECASE)

#: Meta / third-person wording a resume bullet must never contain.
_META_RE = re.compile(
    r"\b(?:the\s+candidate|candidate|profile|resume|curriculum\s+vitae|"
    r"as\s+indicated|as\s+shown|as\s+listed|as\s+noted|as\s+mentioned)\b",
    re.IGNORECASE,
)

#: Zero-width and control characters that hide text from a human reader.
_HIDDEN_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\x00-\x08\x0e-\x1f]")

#: Common resume action verbs. A bullet must start with one of these (or a word that
#: looks like a past/present participle, handled by :func:`_starts_with_action_verb`).
_ACTION_VERBS = frozenset(
    {
        "analysed",
        "analyze",
        "analyzed",
        "analyses",
        "architected",
        "architect",
        "architects",
        "automate",
        "automated",
        "automates",
        "build",
        "built",
        "builds",
        "coached",
        "collaborate",
        "collaborated",
        "collaborates",
        "configure",
        "configured",
        "configures",
        "containerize",
        "containerized",
        "create",
        "created",
        "creates",
        "debug",
        "debugged",
        "debugs",
        "deliver",
        "delivered",
        "delivers",
        "deploy",
        "deployed",
        "deploys",
        "design",
        "designed",
        "designs",
        "develop",
        "developed",
        "develops",
        "document",
        "documented",
        "documents",
        "drove",
        "drive",
        "drives",
        "engineer",
        "engineered",
        "engineers",
        "handle",
        "handled",
        "handles",
        "implement",
        "implemented",
        "implements",
        "improve",
        "improved",
        "improves",
        "initiate",
        "initiated",
        "initiates",
        "integrate",
        "integrated",
        "integrates",
        "launch",
        "launched",
        "leads",
        "lead",
        "led",
        "maintain",
        "maintained",
        "maintains",
        "manage",
        "managed",
        "manages",
        "mentor",
        "mentored",
        "mentors",
        "migrate",
        "migrated",
        "migrates",
        "optimise",
        "optimised",
        "optimize",
        "optimized",
        "orchestrate",
        "orchestrated",
        "paired",
        "partner",
        "partnered",
        "present",
        "presented",
        "presents",
        "provision",
        "provisioned",
        "publish",
        "published",
        "publishes",
        "refactor",
        "refactored",
        "refactors",
        "reduce",
        "reduced",
        "reduces",
        "resolve",
        "resolved",
        "resolves",
        "review",
        "reviewed",
        "reviews",
        "scale",
        "scaled",
        "scales",
        "ship",
        "shipped",
        "ships",
        "spearhead",
        "spearheaded",
        "streamline",
        "streamlined",
        "streamlines",
        "test",
        "tested",
        "tests",
        "write",
        "writes",
        "wrote",
    }
)


# --------------------------------------------------------------------------- #
# Output models
# --------------------------------------------------------------------------- #


class ResumeLine(BaseModel):
    """One sentence/bullet plus the profile fact ids it rests on."""

    text: str
    facts: list[str] = Field(default_factory=list)


class DraftEntry(BaseModel):
    """Bullets the model wrote for one profile experience/project entry."""

    bullets: list[ResumeLine] = Field(default_factory=list)


class RequirementDraft(BaseModel):
    """The model's coverage judgement for one job requirement."""

    requirement: str = ""
    status: str = "missing"
    facts: list[str] = Field(default_factory=list)
    quote: str = ""


class ResumeDraft(BaseModel):
    """Exactly what the model must return (lenient; code validates the rest).

    A stray ``skills`` key from an older prompt is ignored on purpose: skills are
    assembled in code.
    """

    summary: list[ResumeLine] = Field(default_factory=list)
    experience: list[DraftEntry] = Field(default_factory=list)
    projects: list[DraftEntry] = Field(default_factory=list)
    requirements: list[RequirementDraft] = Field(default_factory=list)


class ResumeHeader(BaseModel):
    name: str | None = None
    email: str | None = None
    links: Any = None


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


class RequirementCoverage(BaseModel):
    """How the profile covers one job requirement."""

    requirement: str
    status: str = "missing"  # covered | partial | missing
    facts: list[str] = Field(default_factory=list)
    quote: str = ""


class DroppedLine(BaseModel):
    """A line the grounder rejected, kept for the owner-facing report."""

    text: str
    reason: str


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


class ATSReport(BaseModel):
    passed: bool = True
    reasons: list[str] = Field(default_factory=list)


class TailoredResume(BaseModel):
    """A validated, tailored resume ready to store or export."""

    header: ResumeHeader = Field(default_factory=ResumeHeader)
    summary: list[ResumeLine] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    familiar_skills: list[str] = Field(default_factory=list)
    skill_facts: dict[str, str] = Field(default_factory=dict)
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    requirements: list[RequirementCoverage] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    dropped: list[DroppedLine] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""
    profile_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ats: ATSReport = Field(default_factory=ATSReport)
    prompt_version: str = ""
    model: str = ""


# --------------------------------------------------------------------------- #
# Token / skill helpers
# --------------------------------------------------------------------------- #


def strip_proficiency(name: str) -> str:
    """Drop a trailing "(strong)"/"(working)"/"(familiar)" tag from a skill name."""
    return _PROFICIENCY_SUFFIX_RE.sub("", name or "").strip()


def _singular(token: str) -> str:
    """A crude singular form so "APIs" and "api" compare equal."""
    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("ss"):
        return token
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def _tokenize(text: str) -> list[str]:
    """Split on non-alphanumerics (``Git/GitHub`` -> ``git``, ``github``) + singular."""
    return [_singular(m.group(0).lower()) for m in _TOKEN_RE.finditer(text or "")]


def _canonical(name: str, ontology: SkillOntology) -> str:
    """Alias-aware canonical key, ignoring any proficiency suffix."""
    return ontology.canonicalize(strip_proficiency(name))


def _jd_keyword_tokens(parsed_jd: ParsedJD, ontology: SkillOntology) -> set[str]:
    tokens: set[str] = set()
    for name in (*parsed_jd.must_have_skills, *parsed_jd.nice_to_have_skills):
        for value in (name, _canonical(name, ontology)):
            tokens.update(_tokenize(value))
    return tokens


def _jd_canonical(parsed_jd: ParsedJD, ontology: SkillOntology) -> set[str]:
    return {
        _canonical(name, ontology)
        for name in (*parsed_jd.must_have_skills, *parsed_jd.nice_to_have_skills)
    }


def _dedup_requirements(parsed_jd: ParsedJD, ontology: SkillOntology) -> list[str]:
    ordered = [*parsed_jd.must_have_skills, *parsed_jd.nice_to_have_skills]
    out: list[str] = []
    seen: set[str] = set()
    for name in ordered:
        canonical = _canonical(name, ontology)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(name)
    return out


# --------------------------------------------------------------------------- #
# Profile facts
# --------------------------------------------------------------------------- #


class Fact(BaseModel):
    """One citable profile fact with its stable id."""

    id: str
    text: str
    entry: str
    tech: list[str] = Field(default_factory=list)


def _education_line(education: Any) -> str:
    head = ", ".join(p for p in (education.degree, education.field) if p)
    if education.institution:
        head = f"{head} @ {education.institution}" if head else education.institution
    if education.graduation_year is not None:
        when = "expected" if getattr(education, "expected", False) else "graduated"
        head += f" ({when} {education.graduation_year})"
    return head


def build_facts(profile: CandidateProfile) -> dict[str, Fact]:
    """Numbered, citable facts from the profile (skills are deliberately excluded)."""
    facts: dict[str, Fact] = {}

    def add(fact_id: str, text: str | None, entry: str, tech: list[str]) -> None:
        value = (text or "").strip()
        if value:
            facts[fact_id] = Fact(id=fact_id, text=value, entry=entry, tech=list(tech))

    for index, project in enumerate(profile.projects, start=1):
        key = f"P{index}"
        add(f"{key}.title", project.title, key, project.tech)
        add(f"{key}.summary", project.summary, key, project.tech)
        add(f"{key}.outcomes", project.outcomes, key, project.tech)
        add(f"{key}.tech", ", ".join(project.tech), key, project.tech)
    for label, entries in (("I", profile.internships), ("W", profile.work)):
        for index, experience in enumerate(entries, start=1):
            key = f"{label}{index}"
            tech = experience.tech
            add(f"{key}.role", experience.role, key, tech)
            add(f"{key}.company", experience.company, key, tech)
            add(f"{key}.dates", experience.dates, key, tech)
            add(f"{key}.description", " ".join(experience.description), key, tech)
            add(f"{key}.tech", ", ".join(tech), key, tech)
    for index, education in enumerate(profile.education, start=1):
        add(f"ED{index}.line", _education_line(education), f"ED{index}", [])
    if profile.certifications:
        add("CERT", ", ".join(profile.certifications), "CERT", [])
    link_parts = [
        f"{label}: {value}"
        for label in ("github", "linkedin", "portfolio")
        if (value := getattr(profile.links, label, None))
    ]
    if link_parts:
        add("LINK", "; ".join(link_parts), "LINK", [])
    return facts


def render_profile_facts(profile: CandidateProfile) -> str:
    """The profile as numbered facts the model must cite, plus context-only skills."""
    facts = build_facts(profile)
    lines = ["Candidate facts (cite the [ID] of every fact you use):"]
    for fact in facts.values():
        lines.append(f"[{fact.id}] {fact.text}")
    if profile.skills:
        lines.append("")
        lines.append("Known skills (context only; never cite as evidence):")
        lines.append(
            ", ".join(
                f"{strip_proficiency(skill.name)} "
                f"({getattr(skill.proficiency, 'value', skill.proficiency)})"
                for skill in profile.skills
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Profile gaps (refusal)
# --------------------------------------------------------------------------- #


def profile_gaps(profile: CandidateProfile) -> list[str]:
    """What the profile must add before a resume can be written (no padding)."""
    gaps: list[str] = []
    experiences = [("internship", x) for x in profile.internships] + [
        ("work", x) for x in profile.work
    ]
    for label, experience in experiences:
        if not any(line.strip() for line in experience.description):
            name = f"{experience.role or '?'} @ {experience.company or '?'}"
            gaps.append(f"{label} '{name}' has no description lines")
    if not profile.internships and not profile.work:
        gaps.append("no internship or work experience with a description")
    for project in profile.projects:
        if not (project.summary or project.outcomes):
            gaps.append(f"project '{project.title or '?'}' has no summary or outcomes")
    if not profile.projects:
        gaps.append("no projects")
    if not any((profile.links.github, profile.links.linkedin, profile.links.portfolio)):
        gaps.append("no links (add a github, linkedin or portfolio URL)")
    return gaps


# --------------------------------------------------------------------------- #
# Skills (built in code, never by the model)
# --------------------------------------------------------------------------- #


def _evidence_index(profile: CandidateProfile, ontology: SkillOntology) -> dict[str, str]:
    """Map an evidence token to the fact id that provides it.

    Evidence comes from every project/internship/work *tech list* (its ``.tech`` fact)
    **and** from a project's ``.summary``/``.outcomes`` prose and an internship/work
    ``.description`` prose. So a concept skill (Idempotency, Caching, Circuit Breakers,
    REST API Design, LLM pipeline design) can be evidenced by prose, not only a tech
    list. ``API``/``REST`` and ``AI``/``LLM`` are synonyms on both sides.
    """
    facts = build_facts(profile)
    index: dict[str, str] = {}

    def add(token: str, fact_id: str) -> None:
        key = _SYNONYMS.get(token, token)
        if key and key not in index:
            index[key] = fact_id

    for fact in facts.values():
        if fact.id.endswith(".tech"):
            for tech in fact.tech:
                add(_canonical(tech, ontology), fact.id)
                for token in _tokenize(tech):
                    add(token, fact.id)
        elif fact.id.endswith((".summary", ".outcomes", ".description")):
            for token in _tokenize(fact.text):
                add(token, fact.id)
    return index


def _skill_evidence_fact(skill: Any, index: dict[str, str], ontology: SkillOntology) -> str:
    """The fact id that evidences ``skill``, or ``""`` when nothing does.

    Prefer the skill's canonical form, then any of its name tokens (ignoring generic
    nouns), then — for an all-generic concept name such as "LLM pipeline design" — every
    token of length >= 3.
    """
    canonical = _canonical(skill.name or skill.canonical_name, ontology)
    if canonical in index:
        return index[canonical]
    tokens = [token for token in _tokenize(skill.name) if len(token) > 1]
    meaningful = [token for token in tokens if token not in _GENERIC_TERMS]
    for token in meaningful or [token for token in tokens if len(token) >= 3]:
        if token in index:
            return index[token]
        synonym = _SYNONYMS.get(token, token)
        if synonym in index:
            return index[synonym]
    return ""


def skill_evidence_facts(profile: CandidateProfile, ontology: SkillOntology) -> dict[str, str]:
    """The evidence fact id for each of the owner's skills (``""`` when un-evidenced)."""
    index = _evidence_index(profile, ontology)
    out: dict[str, str] = {}
    for skill in profile.skills:
        name = strip_proficiency(skill.name)
        if name and name not in out:
            out[name] = _skill_evidence_fact(skill, index, ontology)
    return out


def build_skills(
    profile: CandidateProfile, parsed_jd: ParsedJD, ontology: SkillOntology
) -> tuple[list[str], list[str]]:
    """Return ``(skills, familiar)``:

    ``skills`` = JD-matching first, then other skills that are evidenced in a
    project/internship/work tech list or in a project summary/outcomes or an
    internship/work description, or confirmed by the owner. ``familiar`` = the
    "Familiar with (confirm before use)" group: familiar-proficiency skills and any
    skill with no evidence and no confirmation. Names are the profile's own, with any
    proficiency suffix stripped.
    """
    jd_canonical = _jd_canonical(parsed_jd, ontology)
    index = _evidence_index(profile, ontology)
    matching: list[str] = []
    others: list[str] = []
    familiar: list[str] = []
    seen: set[str] = set()
    for skill in profile.skills:
        canonical = _canonical(skill.name or skill.canonical_name, ontology)
        if canonical in seen:
            continue
        seen.add(canonical)
        name = strip_proficiency(skill.name)
        evidenced = skill.confirmed or bool(_skill_evidence_fact(skill, index, ontology))
        if skill.proficiency == Proficiency.familiar or not evidenced:
            familiar.append(name)
        elif canonical in jd_canonical:
            matching.append(name)
        else:
            others.append(name)
    return matching + others, familiar


# --------------------------------------------------------------------------- #
# Grounding engine
# --------------------------------------------------------------------------- #


def _is_sentence_initial(text: str, start: int) -> bool:
    preceding = text[:start].rstrip()
    return not preceding or preceding[-1] in ".!?\n:;"


def _style_error(text: str) -> str:
    match = _META_RE.search(text)
    if match:
        return f"meta/third-person wording: '{match.group(0)}'"
    return ""


def _starts_with_action_verb(text: str) -> bool:
    match = _TOKEN_RE.search(text)
    if not match:
        return False
    word = match.group(0).lower()
    if word in _ACTION_VERBS:
        return True
    if len(word) >= 5 and word.endswith("ed"):
        return True
    return bool(len(word) >= 6 and word.endswith("ing"))


def _keyword_counts(text: str, jd_tokens: set[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for token in _tokenize(text):
        if token in jd_tokens:
            counts[token] += 1
    return counts


def _ungrounded(
    text: str,
    allowed: set[str],
    tech_tokens: set[str],
    ontology: SkillOntology,
) -> list[str]:
    """Tokens that assert a fact absent from the cited text/tech.

    Only a technology (ontology entry), a number or a mid-sentence proper noun needs
    grounding; generic nouns (data, design, system, API, AI, service, platform,
    pipeline) are ignored. ``AI``/``LLM`` and ``API``/``REST`` are treated as synonyms.
    Sentence-initial words and "I" are ignored.
    """
    grounded = set(allowed) | set(tech_tokens)
    grounded |= {_SYNONYMS.get(token, token) for token in grounded}
    flagged: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        raw = match.group(0)
        if raw.lower() == "i" or _is_sentence_initial(text, match.start()):
            continue
        token = _singular(raw.lower())
        if token in _GENERIC_TERMS:
            continue
        if token in grounded or _SYNONYMS.get(token, token) in grounded:
            continue
        if any(ch.isdigit() for ch in raw):
            flagged.append(raw)
            continue
        if _canonical(raw, ontology) in ontology.entries:
            flagged.append(raw)
            continue
        if raw[0].isupper() and len(raw) >= 2:
            flagged.append(raw)
    return sorted(set(flagged))


def _claim_error(text: str, grounded: set[str]) -> str:
    """Reject a strength/ownership claim whose words are absent from the cited facts."""
    line_tokens = set(_tokenize(text))
    grounded = set(grounded) | {_SYNONYMS.get(token, token) for token in grounded}
    for term in _CLAIM_TERMS:
        term_tokens = set(_tokenize(term))
        if term_tokens and term_tokens <= line_tokens and not term_tokens <= grounded:
            return f"claim not supported by cited facts: '{term}'"
    return ""


class _Grounder:
    """Applies the deterministic grounding rules to one resume build."""

    def __init__(
        self,
        *,
        profile: CandidateProfile,
        parsed_jd: ParsedJD,
        ontology: SkillOntology,
    ) -> None:
        self.facts = build_facts(profile)
        self.ontology = ontology
        self.jd_tokens = _jd_keyword_tokens(parsed_jd, ontology)
        self.counts: Counter[str] = Counter()

    def seed_skill_keywords(self, names: list[str]) -> None:
        """The Skills section counts once toward each keyword it lists."""
        self.counts.update(_keyword_counts(" ".join(names), self.jd_tokens))

    def keep_line(self, line: ResumeLine, *, is_bullet: bool) -> tuple[bool, str]:
        """Return ``(keep, reason)`` for one summary sentence or bullet."""
        text = (line.text or "").strip()
        if not text:
            return False, "empty text"
        style = _style_error(text)
        if style:
            return False, style
        if _HIDDEN_RE.search(text):
            return False, "hidden characters in text"
        raw_ids = list(line.facts or [])
        unknown = [fid for fid in raw_ids if fid not in self.facts]
        if unknown:
            return False, f"unknown fact id(s): {', '.join(unknown)}"
        if not raw_ids:
            return False, "no fact id cited"
        cited_text = " ".join(self.facts[fid].text for fid in raw_ids)
        tech = [t for fid in raw_ids for t in self.facts[fid].tech]
        allowed = set(_tokenize(cited_text))
        tech_tokens = set(_tokenize(" ".join(tech)))
        if is_bullet and not _starts_with_action_verb(text):
            return False, "bullet does not start with an action verb"
        flagged = _ungrounded(text, allowed, tech_tokens, self.ontology)
        if flagged:
            return False, f"term(s) not in cited facts: {', '.join(flagged)}"
        claim = _claim_error(text, allowed | tech_tokens)
        if claim:
            return False, claim
        counts = _keyword_counts(text, self.jd_tokens)
        over = [key for key, value in counts.items() if self.counts[key] + value > KEYWORD_MAX]
        if over:
            return False, f"job keyword limit exceeded: {', '.join(sorted(over))}"
        for key, value in counts.items():
            self.counts[key] += value
        return True, ""


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
    if resume.familiar_skills:
        blocks.append(
            ResumeBlock(
                kind=ResumeBlockKind.paragraph,
                text=f"{FAMILIAR_HEADING}: " + ", ".join(resume.familiar_skills),
            )
        )
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
        "## Candidate facts\n"
        f"{render_profile_facts(profile)}\n\n"
        "## Job requirements\n"
        f"{render_parsed_jd(parsed_jd)}\n\n"
        "## Raw job posting\n"
        f"{render_job(job)}"
    )


def _retry_message(errors: list[str]) -> str:
    return (
        "Your previous draft did not pass validation:\n"
        + "\n".join(f"- {error}" for error in errors)
        + "\nFix every item. Use ONLY facts in the candidate facts list; every summary "
        "sentence and bullet must cite at least one [ID]; introduce no technology, "
        "proper noun, number or job keyword the cited facts do not contain; never cite "
        "a skill as evidence."
    )


# --------------------------------------------------------------------------- #
# Requirement classification
# --------------------------------------------------------------------------- #


def _fact_for_concept(concept: str, facts: dict[str, Fact], ontology: SkillOntology) -> Fact | None:
    """The fact that best evidences ``concept``.

    Prefer the fact whose *text* names the concept (the entry's ``.tech`` fact), so the
    cited id and quote actually state it; only then fall back to any fact of an entry
    whose tech list carries the concept, then to a free-text mention.
    """
    tech_match: Fact | None = None
    for fact in facts.values():
        if any(_canonical(tech, ontology) == concept for tech in fact.tech):
            if concept in set(_tokenize(fact.text)):
                return fact
            tech_match = tech_match or fact
    if tech_match is not None:
        return tech_match
    for fact in facts.values():
        if concept in set(_tokenize(fact.text)):
            return fact
    return None


def _profile_concepts(
    profile: CandidateProfile, ontology: SkillOntology, facts: dict[str, Fact]
) -> dict[str, bool]:
    """Map a profile concept to whether it is only ``familiar`` proficiency.

    Concepts come from every project/internship/work tech list (not familiar) and from
    the owner's skills (with their proficiency). A concept evidenced by a tech list is
    never downgraded to familiar-only.
    """
    concepts: dict[str, bool] = {}
    for fact in facts.values():
        for tech in fact.tech:
            concepts.setdefault(_canonical(tech, ontology), False)
    for skill in profile.skills:
        canonical = _canonical(skill.name or skill.canonical_name, ontology)
        familiar = skill.proficiency == Proficiency.familiar
        concepts[canonical] = concepts.get(canonical, True) and familiar
    return concepts


def _family_targets(name: str, ontology: SkillOntology) -> list[str]:
    """The strict concept-family words a requirement may be satisfied by (never the
    requirement's own canonical). An empty tuple means the requirement has no family,
    so it may fall back to a quoted model judgement."""
    canonical = _canonical(name, ontology)
    key = "".join(ch for ch in (name or "").lower().strip() if ch not in {" ", ".", "-", "_", "/"})
    targets: list[str] = []
    for family_key in (key, canonical):
        for target in _REQUIREMENT_FAMILIES.get(family_key, ()):
            if target not in targets:
                targets.append(target)
    return targets


def _targets_for_requirement(name: str, ontology: SkillOntology) -> list[str]:
    targets = [_canonical(name, ontology)]
    for target in _family_targets(name, ontology):
        if target not in targets:
            targets.append(target)
    return targets


def _classify_requirements(
    profile: CandidateProfile,
    parsed_jd: ParsedJD,
    ontology: SkillOntology,
    facts: dict[str, Fact],
    drafts: list[RequirementDraft],
) -> list[RequirementCoverage]:
    """Classify each requirement covered / partial / missing.

    Ontology, alias-map and concept-family matching run first (so "REST API Design"
    covers "APIs", "Circuit Breakers"/"Idempotency" cover design patterns, and
    "PostgreSQL"/"Redis" cover storage). A familiar concept yields ``partial``. Only a
    free-text judgement from the model needs an 8-character verbatim quote; a skill or
    tech name does not. A requirement with a concept family is judged *only* by that
    family and the model's judgement can never upgrade it: "data structures" stays
    missing unless a fact actually names a data structure or algorithm.
    """
    by_canonical: dict[str, RequirementDraft] = {}
    for item in drafts:
        by_canonical.setdefault(_canonical(item.requirement, ontology), item)
    concepts = _profile_concepts(profile, ontology, facts)
    results: list[RequirementCoverage] = []
    for name in _dedup_requirements(parsed_jd, ontology):
        canonical = _canonical(name, ontology)
        has_family = bool(_family_targets(name, ontology))
        coverage: RequirementCoverage | None = None
        fallback: RequirementCoverage | None = None
        for target in _targets_for_requirement(name, ontology):
            familiar = concepts.get(target, False)
            found = _fact_for_concept(target, facts, ontology)
            if found is None and target not in concepts:
                continue
            fact_id = found.id if found is not None else ""
            status = "partial" if (familiar or not fact_id) else "covered"
            candidate = RequirementCoverage(
                requirement=name,
                status=status,
                facts=[fact_id] if fact_id else [],
                quote=found.text if found is not None else "",
            )
            if fact_id:
                coverage = candidate
                break
            if fallback is None:
                fallback = candidate
        if coverage is None and not has_family:
            draft = by_canonical.get(canonical)
            if draft is not None and draft.status in ("covered", "partial"):
                cited = [facts[fid] for fid in draft.facts if fid in facts]
                if cited:
                    cited_text = " ".join(fact.text for fact in cited)
                    if draft.quote and quote_valid(
                        draft.quote, EvidenceSource.profile, "", cited_text
                    ):
                        coverage = RequirementCoverage(
                            requirement=name,
                            status=draft.status,
                            facts=[fact.id for fact in cited],
                            quote=draft.quote,
                        )
        if coverage is None:
            coverage = fallback
        if coverage is None:
            coverage = RequirementCoverage(requirement=name, status="missing")
        results.append(coverage)
    return results


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _keep_lines(
    lines: list[ResumeLine],
    grounder: _Grounder,
    label: str,
    *,
    is_bullet: bool,
    dropped: list[DroppedLine],
) -> list[ResumeLine]:
    kept: list[ResumeLine] = []
    for line in lines:
        keep, reason = grounder.keep_line(line, is_bullet=is_bullet)
        if keep:
            kept.append(ResumeLine(text=line.text.strip(), facts=list(line.facts)))
        else:
            logger.warning("tailored-resume %s dropped: %s", label, reason)
            dropped.append(DroppedLine(text=(line.text or "").strip(), reason=reason))
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
    ontology: SkillOntology,
) -> TailoredResume:
    grounder = _Grounder(profile=profile, parsed_jd=parsed_jd, ontology=ontology)
    skills, familiar = build_skills(profile, parsed_jd, ontology)
    skill_facts = skill_evidence_facts(profile, ontology)
    grounder.seed_skill_keywords([*skills, *familiar])

    dropped: list[DroppedLine] = []
    summary = _keep_lines(draft.summary, grounder, "summary", is_bullet=False, dropped=dropped)[
        :SUMMARY_MAX
    ]

    profile_experience = [*profile.internships, *profile.work]
    experience: list[ExperienceEntry] = []
    for index, entry in enumerate(profile_experience):
        bullets = draft.experience[index].bullets if index < len(draft.experience) else []
        experience.append(
            ExperienceEntry(
                role=entry.role,
                company=entry.company,
                duration_months=entry.duration_months,
                bullets=_keep_lines(
                    bullets,
                    grounder,
                    f"experience[{index}]",
                    is_bullet=True,
                    dropped=dropped,
                ),
            )
        )

    projects: list[ProjectEntry] = []
    for index, project in enumerate(profile.projects):
        bullets = draft.projects[index].bullets if index < len(draft.projects) else []
        projects.append(
            ProjectEntry(
                title=project.title,
                tech=list(project.tech),
                bullets=_keep_lines(
                    bullets,
                    grounder,
                    f"projects[{index}]",
                    is_bullet=True,
                    dropped=dropped,
                ),
            )
        )

    requirements = _classify_requirements(
        profile, parsed_jd, ontology, grounder.facts, draft.requirements
    )
    missing = [item.requirement for item in requirements if item.status == "missing"]

    warnings: list[str] = []
    internship_bullets = sum(
        len(experience[index].bullets) for index in range(len(profile.internships))
    )
    if profile.internships and internship_bullets < 2:
        warnings.append("internship section thin: add detail on the Profile page")

    resume = TailoredResume(
        header=ResumeHeader(name=profile.full_name, email=profile.email, links=profile.links),
        summary=summary,
        skills=skills,
        familiar_skills=familiar,
        skill_facts=skill_facts,
        experience=experience,
        projects=projects,
        education=[edu.model_dump() for edu in profile.education],
        certifications=list(profile.certifications),
        requirements=requirements,
        missing_requirements=missing,
        dropped=dropped,
        warnings=warnings,
        prompt_version=version,
        model=model,
    )
    resume.ats = ats_lint(resume)
    return resume


def _refusal(
    profile: CandidateProfile, version: str, model: str, gaps: list[str]
) -> TailoredResume:
    reason = (
        "Refusing to write a resume: the approved profile is missing detail the "
        "generator needs. Add on the Profile page: " + "; ".join(gaps) + "."
    )
    return TailoredResume(
        header=ResumeHeader(name=profile.full_name, email=profile.email, links=profile.links),
        education=[edu.model_dump() for edu in profile.education],
        certifications=list(profile.certifications),
        refused=True,
        refusal_reason=reason,
        profile_gaps=list(gaps),
        prompt_version=version,
        model=model,
    )


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
    model = settings.models.evaluator

    gaps = profile_gaps(profile)
    if gaps:
        logger.warning("tailored-resume refused: %s", "; ".join(gaps))
        return _refusal(profile, version, model, gaps)

    base_user = build_user_prompt(profile, job, parsed_jd)
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
