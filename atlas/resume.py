"""Grounded tailored-resume generator (Phase 8a, PRs C1/C1b).

Given the approved profile and one parsed job, draft a resume tailored to that job.
Grounding is by *fact IDs*, not free-text quotes:

* the profile is rendered to the model as numbered facts with stable ids; prose fields
  are split into sentence facts (``[P1.summary.s1]``, ``[I1.s1]``, ``[ED1.line]``, ...);
* every summary sentence and bullet must cite one or more of those ids;
* code maps each id back to its verbatim text and verifies that every technology,
  proper noun and number in the line appears in the cited text (or that entry's tech
  list), so a line cannot add a claim the cited facts do not contain;
* every content word of a line (nouns/adjectives/adverbs, minus the leading verb, stop
  words and a short neutral list) must also appear in the cited text by stem or through a
  small synonym map; more than one ungrounded content word drops the line;
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

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import add_llm_tokens, find_cached_tailored_resume, save_tailored_resume
from .evidence import quote_valid
from .llm import LLMClient
from .prompt_store import load_prompt
from .render import render_job, render_parsed_jd
from .schemas import CandidateProfile, EvidenceSource, Job, ParsedJD, Proficiency
from .skills import SkillOntology, get_ontology
from .text import clean_tech_items

logger = logging.getLogger(__name__)

#: The model is re-prompted at most this many times before the leftover fails are
#: simply dropped.
MAX_RESUME_RETRIES = 2

#: A summary of more than this many sentences is trimmed to this many.
SUMMARY_MAX = 3

#: A job keyword may appear at most this many times across the whole resume; the
#: Skills section counts once toward each keyword it lists.
KEYWORD_MAX = 3

#: Fewer than this many grounded internship bullets triggers the one repair pass.
MIN_INTERNSHIP_BULLETS = 3

#: The most LLM calls (draft + repairs) one resume build may ever make. The draft
#: loop and the per-section repair loops all draw from this single budget, so a
#: build can never run away on cost.
MAX_RESUME_CALLS = 3

#: How many repair calls a single thin/empty section may use before falling back to
#: a verbatim profile sentence. Bounded further by :data:`MAX_RESUME_CALLS`.
MAX_SECTION_REPAIR_ATTEMPTS = 2

#: The main Skills group holds at most this many names; any overflow moves to the
#: "Additional tools" list (shown on the review page, not exported by default).
MAX_MAIN_SKILLS = 14

#: A model summary sentence is dropped when it overlaps any bullet by at least this
#: much (Jaccard over content-word keys); the first summary line is built in code.
SUMMARY_OVERLAP_MAX = 0.6

#: Skills that are hosting/observability platforms rather than engineering skills:
#: kept out of the main group unless the JD names them. "neon" is also an alias of
#: PostgreSQL, so it normally collapses away before this list is consulted.
_HOSTING_OBSERVABILITY: frozenset[str] = frozenset(
    {"render", "vercel", "braintrust", "neon", "netlify", "heroku", "railway"}
)

#: Surface skill spellings that mean the same thing; every key maps to one display
#: name so the main group never lists an entry twice under two spellings.
_SKILL_ALIASES: dict[str, str] = {
    "jwt": "JWT",
    "jwt auth": "JWT",
    "postgresql": "PostgreSQL",
    "postgresql (neon)": "PostgreSQL",
    "neon": "PostgreSQL",
    "github actions": "GitHub Actions",
    "ci/cd (github actions)": "GitHub Actions",
    "ci/cd": "GitHub Actions",
    "git/github": "GitHub Actions",
}

#: Month names for :func:`format_dates` (no locale dependency).
_MONTHS: tuple[str, ...] = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_LOOKUP: dict[str, int] = {
    name[:3].lower(): index for index, name in enumerate(_MONTHS) if name
}


#: Headings an ATS expects, in order. A clean layout has all of them.
STANDARD_HEADINGS: tuple[str, ...] = ("Summary", "Skills", "Experience", "Projects", "Education")

#: Heading for the second, lower-confidence skill group.
FAMILIAR_HEADING = "Familiar with (confirm before use)"

#: ``ResumeLine.source`` marker for a code-supplied fallback copied from the profile.
PROFILE_VERBATIM = "profile-verbatim"

#: Words that claim ownership, leadership, reliability or scale. Single-word stems are
#: matched as a *prefix* of a line token (so "delivering" is caught by "deliver" and
#: "scalable" by "scal"), and a stem is allowed only when the same stem appears in the
#: cited fact. Multi-word phrases must appear whole. "led" is matched as a whole token so
#: "Apex Ledger" cannot license it. A "Sole engineer" fact cannot license "led" or
#: "architected", and a plain project cannot license "production-grade", "from scratch",
#: "core", "enterprise", "scalable", "end-to-end" or "delivered".
_CLAIM_TERMS: tuple[str, ...] = (
    "deliver",
    "own",
    "lead",
    "led",
    "architect",
    "manage",
    "mentor",
    "robust",
    "reliab",
    "scal",
    "production-grade",
    "from scratch",
    "core",
    "enterprise",
    "end-to-end",
    "zero-downtime",
    "high-scale",
    "millions",
    "expert",
)

#: Claim terms matched as a whole token (never a prefix).
_CLAIM_EXACT: frozenset[str] = frozenset({"led"})

#: Multi-word claim phrases, matched as a whole token set.
_CLAIM_PHRASES: tuple[str, ...] = tuple(term for term in _CLAIM_TERMS if " " in term or "-" in term)

#: Single-word claim stems, matched as a prefix of a line token.
_CLAIM_PREFIXES: tuple[str, ...] = tuple(
    term for term in _CLAIM_TERMS if term not in _CLAIM_EXACT and term not in _CLAIM_PHRASES
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

#: Stop words a content-word grounding check ignores (function words, not facts).
_STOPWORDS: frozenset[str] = frozenset(
    "a am an and any are as at be been being below between both but by can could did do does "  # noqa: SIM905 - one word list reads better than one word per line
    "doing done down during each even every few for from further get gets got had has have having "
    "he her here hers him his how i in into is it its just make makes many may me might mine more "
    "most much must my no nor not of off on once only onto or other others our ours out over per "
    "shall she should so some such than that the their theirs them then there these they this those "
    "through to too toward towards under until up upon use used uses using very via we well were "
    "what when where which while who whom whose why will with within without would yet you your yours".split()
)

#: Content words that never need grounding: the generic nouns plus a few structural
#: nouns (system, service, application, feature) the owner named as neutral.
_NEUTRAL_CONTENT: frozenset[str] = _GENERIC_TERMS | frozenset({"feature", "features"})

#: A small synonym map so equivalent verbs and words ground each other. Keys are the
#: singular/stemmed surface forms; every value is the canonical content key.
_CONTENT_SYNONYMS: dict[str, str] = {
    "build": "implement",
    "built": "implement",
    "creat": "implement",
    "create": "implement",
    "develop": "implement",
    "implement": "implement",
    "api": "endpoint",
    "endpoint": "endpoint",
    "llm": "ai",
    "ai": "ai",
}

#: Data structures and algorithms a "data structures" requirement may lean on. Only
#: the noun forms are matched (see ``_data_structure_named``): a plural that is also a
#: third-person verb ("queues them") does not count unless a premodifier makes it a
#: noun ("message queues").
_DATA_STRUCTURE_TERMS: tuple[str, ...] = (
    "array",
    "hashmap",
    "hashtable",
    "dictionary",
    "tree",
    "graph",
    "queue",
    "stack",
    "heap",
    "linkedlist",
    "trie",
    "sorting",
    "binarysearch",
    "recursion",
    "dynamicprogramming",
    "algorithm",
)

_DATA_STRUCTURE_TERM_SET: frozenset[str] = frozenset(_DATA_STRUCTURE_TERMS)

#: Words that, immediately before a plural data-structure noun, mark it as a noun
#: rather than a verb ("message queues", "multiple stacks"). Relative/demonstrative
#: pronouns are deliberately absent: "that queues ..." is a verb.
_DS_NOUN_PREMODIFIERS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "its",
        "our",
        "my",
        "their",
        "his",
        "her",
        "multiple",
        "several",
        "two",
        "three",
        "four",
        "five",
        "both",
        "all",
        "no",
        "any",
        "some",
        "priority",
        "message",
        "call",
        "binary",
        "linked",
        "hash",
        "double",
        "circular",
        "blocking",
        "concurrent",
        "distributed",
        "task",
        "job",
        "work",
        "event",
        "data",
    }
)

#: Singular (always a noun form) and plural (only after a premodifier) surface forms.
_DS_SINGULAR_PLURAL: dict[str, tuple[str, str | None]] = {
    "array": ("array", "arrays"),
    "hashmap": ("hashmap", "hashmaps"),
    "hashtable": ("hashtable", "hashtables"),
    "dictionary": ("dictionary", "dictionaries"),
    "tree": ("tree", "trees"),
    "graph": ("graph", "graphs"),
    "queue": ("queue", "queues"),
    "stack": ("stack", "stacks"),
    "heap": ("heap", "heaps"),
    "trie": ("trie", "tries"),
    "sorting": ("sorting", None),
    "recursion": ("recursion", None),
    "algorithm": ("algorithm", "algorithms"),
}

#: Words that turn a following ambiguous word into a tech-stack phrase rather than a
#: data structure ("full stack", "front stack"), so its occurrence is ignored.
_DS_TECH_STACK_MODIFIERS: frozenset[str] = frozenset(
    {"full", "front", "back", "tech", "technology", "software", "web", "mean", "protocol"}
)

#: Multi-word data-structure phrases, matched as consecutive raw tokens.
_DS_PHRASES: dict[str, tuple[str, ...]] = {
    "binarysearch": ("binary", "search"),
    "linkedlist": ("linked", "list"),
    "dynamicprogramming": ("dynamic", "programming"),
}

#: Surface words, keeping hyphenated compounds together (``full-stack`` is one word,
#: not a data-structure "stack").
_RAW_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-\u2010\u2011][A-Za-z0-9]+)*")

#: How a job requirement is covered by a profile concept family. Keys are
#: key-normalised requirement wordings; values are words that must be *named* in a
#: project/internship/work fact (its tech list or its summary/outcomes/description
#: prose) to satisfy the requirement. A family is deliberately narrow: "PostgreSQL"
#: satisfies "storage", not "system design patterns"; a caching/queue fact satisfies
#: "data structures" only if it names a data structure or algorithm *as a noun* (so the
#: verb in "queues high-volume events" does not count, but "a priority queue" does).
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
    #: ``"profile-verbatim"`` marks a code-supplied fallback copied word-for-word from
    #: the profile; such a line is exempt from the action-verb rule.
    source: str = ""


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


class ResumeRepair(BaseModel):
    """A repair pass: replacement bullets for the thin sections only.

    ``experience``/``projects`` are aligned to the profile's entries (same order as the
    draft); a section that is not thin stays empty.
    """

    experience: list[DraftEntry] = Field(default_factory=list)
    projects: list[DraftEntry] = Field(default_factory=list)


class ResumeHeader(BaseModel):
    name: str | None = None
    email: str | None = None
    links: Any = None


class ExperienceEntry(BaseModel):
    """A profile experience entry copied verbatim, with tailored bullets."""

    role: str | None = None
    company: str | None = None
    duration_months: int | None = None
    #: The stored free-text dates rendered in proper case for output only.
    dates: str | None = None
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
    #: The section label the line came from (``projects[1]``, ``experience[0]``, ...),
    #: so a later per-section repair can list only that section's failures.
    section: str = ""


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
    #: Skills that did not fit the main group (over the cap), or hosting/observability
    #: tools the JD did not name. Shown on the review page; not exported unless picked.
    additional_tools: list[str] = Field(default_factory=list)
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


def _raw_words(text: str) -> list[str]:
    """Lowercased surface words, *not* singularised, for noun-form matching."""
    return [m.group(0).lower() for m in _RAW_WORD_RE.finditer(text or "")]


def _stem(token: str) -> str:
    """A crude stem so "enabling"/"enabled" compare with "enable"."""
    value = _singular(token or "")
    for suffix in ("ing", "ed", "ly", "ment", "ion"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 3:
            return value[: -len(suffix)]
    return value


def _content_key(token: str) -> str:
    """The comparable key for a content word (synonyms first, then a light stem)."""
    stemmed = _stem(token)
    return _CONTENT_SYNONYMS.get(stemmed, stemmed)


def _data_structure_named(text: str, term: str) -> bool:
    """Whether ``text`` names the data structure ``term`` as a noun.

    Singular forms always count (the owner accepts a bare "queue"). A plural that is
    also a third-person verb ("queues them") counts only when the preceding word marks
    it as a noun ("message queues"), so "queues high-volume events" does not count.
    """
    words = _raw_words(text)
    phrase = _DS_PHRASES.get(term)
    if phrase is not None:
        span = len(phrase)
        return any(tuple(words[i : i + span]) == phrase for i in range(len(words) - span + 1))
    pair = _DS_SINGULAR_PLURAL.get(term)
    if pair is None:
        return False
    singular, plural = pair
    for index, word in enumerate(words):
        previous = words[index - 1] if index > 0 else ""
        if word == singular:
            if previous in _DS_TECH_STACK_MODIFIERS:
                continue
            return True
        if plural is not None and word == plural and previous in _DS_NOUN_PREMODIFIERS:
            return True
    return False


def _fact_mentions(text: str, concept: str) -> bool:
    """Whether a fact's text names ``concept``; data structures need a noun form."""
    if concept in _DATA_STRUCTURE_TERM_SET:
        return _data_structure_named(text, concept)
    return concept in set(_tokenize(text))


def _canonical(name: str, ontology: SkillOntology) -> str:
    """Alias-aware canonical key, ignoring any proficiency suffix."""
    return ontology.canonicalize(strip_proficiency(name))


def _skill_alias(name: str) -> str:
    """Collapse owner-listed skill aliases to one display name (else the name itself).

    Handles both the full surface ("JWT Auth") and its parenthetical parts, so
    "PostgreSQL (Neon)" and "CI/CD (GitHub Actions)" collapse with their base too.
    """
    value = strip_proficiency(name).strip()
    for candidate in (value, *_expand_tech_item(value)):
        mapped = _SKILL_ALIASES.get(candidate.lower())
        if mapped:
            return mapped
    return value


def _skill_canonical(name: str) -> str:
    """Display-canonical key for deduping a skill name (alias map wins over ontology)."""
    return _skill_alias(name).lower()


def _jd_names_skill(name: str, jd_canonical: set[str]) -> bool:
    """Whether the JD names ``name`` (by canonical key or by its surface tokens)."""
    key = _skill_canonical(name)
    if key in jd_canonical:
        return True
    tokens = set(_tokenize(_skill_alias(name)))
    return bool(tokens and tokens <= jd_canonical)


def _content_keys(text: str) -> set[str]:
    """Content-word keys of a line: nouns/adjectives/adverbs, minus the leading verb.

    Used only for the summary/bullet overlap check, so it is deliberately coarse:
    stop words and the neutral generic nouns are ignored, and equivalent words share
    a key through :data:`_CONTENT_SYNONYMS`.
    """
    tokens = _tokenize(text)
    keys: set[str] = set()
    for index, token in enumerate(tokens):
        if index == 0 or token in _STOPWORDS or token in _NEUTRAL_CONTENT:
            continue
        keys.add(_content_key(token))
    return keys


def _overlap_ratio(text: str, others: list[str]) -> float:
    """The largest Jaccard overlap between ``text`` and any of ``others`` (0 when none).

    Two lines overlap fully when they share every content word and partly otherwise;
    the caller drops the second summary line once this reaches
    :data:`SUMMARY_OVERLAP_MAX`.
    """
    keys = _content_keys(text)
    if not keys:
        return 0.0
    best = 0.0
    for other in others:
        other_keys = _content_keys(other)
        union = keys | other_keys
        if not union:
            continue
        best = max(best, len(keys & other_keys) / len(union))
    return best


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
    kind: str = "field"


def _education_line(education: Any) -> str:
    head = ", ".join(p for p in (education.degree, education.field) if p)
    if education.institution:
        head = f"{head} @ {education.institution}" if head else education.institution
    if education.graduation_year is not None:
        when = "expected" if getattr(education, "expected", False) else "graduated"
        head += f" ({when} {education.graduation_year})"
    return head


#: A single date part: ISO ``2026-03-23``, year-month ``2026-03``, or ``Mar 2026``.
_ISO_DATE_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{1,2})(?:-(?P<day>\d{1,2}))?$")
_MONTH_DATE_RE = re.compile(r"^(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<year>\d{4})$")
_DATE_SPLIT_RE = re.compile(r"\s+(?:-|–|—|to)\s+", re.IGNORECASE)


def _format_date_part(part: str) -> str:
    """One date part in proper case ("2026-03-23" -> "March 23, 2026")."""
    text = part.strip()
    iso = _ISO_DATE_RE.match(text)
    if iso:
        month = int(iso.group("month"))
        if 1 <= month <= 12:
            if iso.group("day"):
                return f"{_MONTHS[month]} {int(iso.group('day'))}, {iso.group('year')}"
            return f"{_MONTHS[month]} {iso.group('year')}"
    named = _MONTH_DATE_RE.match(text)
    if named:
        named_month = _MONTH_LOOKUP.get(named.group("mon")[:3].lower())
        if named_month:
            return f"{_MONTHS[named_month]} {named.group('year')}"
    return text


def format_dates(value: str | None) -> str:
    """Render stored free-text dates in proper case for output only (verbatim if unclear).

    ``"2026-03-23 - 2026-06-30"`` -> ``"March 23, 2026 - June 30, 2026"``. Only the
    rendered output changes; the stored fact keeps the owner's original string.
    """
    text = (value or "").strip()
    if not text:
        return ""
    parts = _DATE_SPLIT_RE.split(text)
    return " - ".join(_format_date_part(part) for part in parts)


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences so each can be cited on its own."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [part.strip() for part in parts if part.strip()]


def build_facts(profile: CandidateProfile) -> dict[str, Fact]:
    """Numbered, citable facts from the profile (skills are deliberately excluded).

    Prose fields are split into sentence facts (``I1.s1``, ``P1.summary.s1``,
    ``P1.outcomes.s2`` ...) so a bullet cites the exact sentence it rests on and cannot
    borrow unrelated detail from the rest of the field. Each fact carries a ``kind``
    (``tech``/``prose``/``field``) used to route evidence and coverage.
    """
    facts: dict[str, Fact] = {}

    def add(fact_id: str, text: str | None, entry: str, tech: list[str], kind: str) -> None:
        value = (text or "").strip()
        if value:
            facts[fact_id] = Fact(id=fact_id, text=value, entry=entry, tech=list(tech), kind=kind)

    def add_sentences(prefix: str, text: str | None, entry: str, tech: list[str]) -> None:
        for index, sentence in enumerate(_split_sentences(text or ""), start=1):
            add(f"{prefix}.s{index}", sentence, entry, tech, "prose")

    for index, project in enumerate(profile.projects, start=1):
        key = f"P{index}"
        tech = clean_tech_items(project.tech)
        add(f"{key}.title", project.title, key, tech, "field")
        add_sentences(f"{key}.summary", project.summary, key, tech)
        add_sentences(f"{key}.outcomes", project.outcomes, key, tech)
        add(f"{key}.tech", ", ".join(tech), key, tech, "tech")
    for label, entries in (("I", profile.internships), ("W", profile.work)):
        for index, experience in enumerate(entries, start=1):
            key = f"{label}{index}"
            tech = clean_tech_items(experience.tech)
            add(f"{key}.role", experience.role, key, tech, "field")
            add(f"{key}.company", experience.company, key, tech, "field")
            add(f"{key}.dates", experience.dates, key, tech, "field")
            add_sentences(key, " ".join(experience.description), key, tech)
            add(f"{key}.tech", ", ".join(tech), key, tech, "tech")
    for index, education in enumerate(profile.education, start=1):
        add(f"ED{index}.line", _education_line(education), f"ED{index}", [], "field")
    if profile.certifications:
        add("CERT", ", ".join(profile.certifications), "CERT", [], "field")
    link_parts = [
        f"{label}: {value}"
        for label in ("github", "linkedin", "portfolio")
        if (value := getattr(profile.links, label, None))
    ]
    if link_parts:
        add("LINK", "; ".join(link_parts), "LINK", [], "field")
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
    **and** from a project's summary/outcomes prose and an internship/work description
    prose (now split into sentence facts). So a concept skill (Idempotency, Caching,
    Circuit Breakers, REST API Design, LLM pipeline design) can be evidenced by prose, not
    only a tech list. ``API``/``REST`` and ``AI``/``LLM`` are synonyms on both sides.
    """
    facts = build_facts(profile)
    index: dict[str, str] = {}

    def add(token: str, fact_id: str) -> None:
        key = _SYNONYMS.get(token, token)
        if key and key not in index:
            index[key] = fact_id

    for fact in facts.values():
        if fact.kind == "tech":
            for tech in fact.tech:
                add(_canonical(tech, ontology), fact.id)
                for token in _tokenize(tech):
                    add(token, fact.id)
        elif fact.kind == "prose":
            for token in _tokenize(fact.text):
                add(token, fact.id)
    return index


def _evidence_fact_for_name(name: str, index: dict[str, str], ontology: SkillOntology) -> str:
    """The fact id in ``index`` that evidences a skill name, or ``""``.

    Prefer the canonical form, then any of the name's tokens (ignoring generic nouns),
    then — for an all-generic concept name such as "LLM pipeline design" — every token of
    length >= 3.
    """
    canonical = _canonical(name, ontology)
    if canonical in index:
        return index[canonical]
    tokens = [token for token in _tokenize(name) if len(token) > 1]
    meaningful = [token for token in tokens if token not in _GENERIC_TERMS]
    for token in meaningful or [token for token in tokens if len(token) >= 3]:
        if token in index:
            return index[token]
        synonym = _SYNONYMS.get(token, token)
        if synonym in index:
            return index[synonym]
    return ""


def _skill_evidence_fact(skill: Any, index: dict[str, str], ontology: SkillOntology) -> str:
    """The fact id that evidences ``skill``, or ``""`` when nothing does."""
    return _evidence_fact_for_name(skill.name or skill.canonical_name, index, ontology)


def _all_tech_items(profile: CandidateProfile) -> list[str]:
    """Every project/internship/work tech item, cleaned and deduplicated in order."""
    items: list[str] = []
    for project in profile.projects:
        items.extend(project.tech)
    for experience in (*profile.internships, *profile.work):
        items.extend(experience.tech)
    return clean_tech_items(items)


#: A trailing parenthetical on a tech item ("PostgreSQL (Neon)" -> base + "Neon").
_TECH_PAREN_RE = re.compile(r"^(?P<base>.*?)\s*[\(\[]\s*(?P<inner>[^\)\]]+?)\s*[\)\]]\s*$")


def _expand_tech_item(item: str) -> list[str]:
    """A tech item plus its parenthetical base/inner so "PostgreSQL (Neon)" yields Neon."""
    out = [item]
    match = _TECH_PAREN_RE.match(item)
    if match:
        for part in (match.group("base").strip(), match.group("inner").strip()):
            if part and part not in out:
                out.append(part)
    return out


def _tech_skill_names(profile: CandidateProfile, ontology: SkillOntology) -> list[str]:
    """Skill names taken straight from the project/internship tech lists, in order.

    Alias spellings collapse to one display name (so "Git/GitHub" and
    "CI/CD (GitHub Actions)" yield a single "GitHub Actions" entry).
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in _all_tech_items(profile):
        for candidate in _expand_tech_item(item):
            value = candidate.strip()
            if not value:
                continue
            key = _skill_canonical(value)
            if key in seen:
                continue
            seen.add(key)
            out.append(_skill_alias(value))
    return out


def skill_evidence_facts(profile: CandidateProfile, ontology: SkillOntology) -> dict[str, str]:
    """The evidence fact id for each shown skill (owner skills + tech-list skills)."""
    index = _evidence_index(profile, ontology)
    out: dict[str, str] = {}
    for skill in profile.skills:
        name = strip_proficiency(skill.name)
        if name and name not in out:
            out[name] = _skill_evidence_fact(skill, index, ontology)
    for name in _tech_skill_names(profile, ontology):
        if name and name not in out:
            out[name] = _evidence_fact_for_name(name, index, ontology)
    return out


def _all_skill_names(profile: CandidateProfile, ontology: SkillOntology) -> list[str]:
    """Every candidate skill name (owner skills + tech-list names), deduped on display."""
    out: list[str] = []
    seen: set[str] = set()
    for skill in profile.skills:
        name = strip_proficiency(skill.name or skill.canonical_name)
        key = _skill_canonical(name)
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    for name in _tech_skill_names(profile, ontology):
        key = _skill_canonical(name)
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _skill_fact_counts(profile: CandidateProfile, ontology: SkillOntology) -> dict[str, int]:
    """How many profile facts evidence each skill, keyed by display-canonical name.

    A fact evidences a skill when the skill's own words appear in the fact's text or its
    tech list. Used only to order the main group (most-evidenced first).
    """
    counts: dict[str, int] = {}
    fact_tokens: list[set[str]] = []
    for fact in build_facts(profile).values():
        fact_tokens.append(set(_tokenize(fact.text)) | set(_tokenize(" ".join(fact.tech))))
    for name in _all_skill_names(profile, ontology):
        key = _skill_canonical(name)
        tokens = set(_tokenize(_skill_alias(name)))
        if not tokens:
            continue
        counts[key] = sum(1 for tokens_in_fact in fact_tokens if tokens & tokens_in_fact)
    return counts


def _is_hosting_observability(name: str) -> bool:
    """Whether a skill is a hosting/observability tool rather than an engineering skill."""
    return _skill_canonical(name) in _HOSTING_OBSERVABILITY


def classify_skills(
    profile: CandidateProfile, parsed_jd: ParsedJD, ontology: SkillOntology
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(skills, familiar, additional_tools)``:

    ``skills`` (main group) holds at most :data:`MAX_MAIN_SKILLS` names: JD-matching
    evidenced skills first, then the other evidenced skills by how many profile facts
    cite them. A skill is main-eligible when it is confirmed, or evidenced in a
    project/internship/work **tech list**, or evidenced with a non-familiar proficiency;
    otherwise it is "familiar". Alias spellings collapse to one entry. Hosting and
    observability tools go to ``additional_tools`` unless the JD names them; any main
    skill past the cap overflows there too. ``additional_tools`` is shown on the review
    page only and is not exported unless the owner selects it.
    """
    jd_canonical = _jd_canonical(parsed_jd, ontology)
    index = _evidence_index(profile, ontology)
    tech_index: dict[str, str] = {}
    for fact in build_facts(profile).values():
        if fact.kind != "tech":
            continue
        for tech in fact.tech:
            for value in (_canonical(tech, ontology), *_tokenize(tech)):
                key = _SYNONYMS.get(value, value)
                if key and key not in tech_index:
                    tech_index[key] = fact.id

    fact_counts = _skill_fact_counts(profile, ontology)
    matching: list[str] = []
    others: list[str] = []
    familiar: list[str] = []
    additional: list[str] = []
    seen: set[str] = set()

    def place(name: str) -> None:
        key = _skill_canonical(name)
        if key in seen:
            return
        seen.add(key)
        if _is_hosting_observability(name) and not _jd_names_skill(name, jd_canonical):
            additional.append(name)
        elif key in jd_canonical:
            matching.append(name)
        else:
            others.append(name)

    for skill in profile.skills:
        name = strip_proficiency(skill.name or skill.canonical_name)
        if _skill_canonical(name) in seen:
            continue
        evidenced = bool(_skill_evidence_fact(skill, index, ontology))
        tech_evidenced = bool(_skill_evidence_fact(skill, tech_index, ontology))
        if (
            skill.confirmed
            or tech_evidenced
            or (evidenced and skill.proficiency != Proficiency.familiar)
        ):
            place(name)
        else:
            seen.add(_skill_canonical(name))
            familiar.append(name)

    for name in _tech_skill_names(profile, ontology):
        place(name)

    others.sort(key=lambda name: fact_counts.get(_skill_canonical(name), 0), reverse=True)
    ordered = [*matching, *others]
    main = ordered[:MAX_MAIN_SKILLS]
    overflow = ordered[MAX_MAIN_SKILLS:]
    for name in overflow:
        if _skill_canonical(name) not in {_skill_canonical(item) for item in additional}:
            additional.append(name)
    return main, familiar, additional


def build_skills(
    profile: CandidateProfile, parsed_jd: ParsedJD, ontology: SkillOntology
) -> tuple[list[str], list[str]]:
    """``(skills, familiar)`` from :func:`classify_skills` (the main and familiar groups)."""
    main, familiar, _additional = classify_skills(profile, parsed_jd, ontology)
    return main, familiar


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


def _ungrounded_content(text: str, grounded_tokens: set[str]) -> list[str]:
    """Content words (nouns/adjectives/adverbs) absent from the cited facts.

    Ignore the leading verb (index 0), stop words and the neutral generic nouns. Two
    words ground each other when they share a stem or a small synonym
    (built/created/developed/implemented, API/endpoint, LLM/AI). The caller drops the line
    when more than one content word is ungrounded.
    """
    tokens = _tokenize(text)
    if not tokens:
        return []
    grounded_keys = {_content_key(token) for token in grounded_tokens}
    flagged: list[str] = []
    for index, token in enumerate(tokens):
        if index == 0:
            continue
        if token in _STOPWORDS or token in _NEUTRAL_CONTENT:
            continue
        if _content_key(token) in grounded_keys:
            continue
        flagged.append(token)
    return sorted(set(flagged))


def _claim_error(text: str, grounded: set[str]) -> str:
    """Reject a strength/ownership claim whose words are absent from the cited facts.

    A single-word stem matches any line token that *starts with* the stem, and is licensed
    only by a cited token that starts with the same stem, so "delivering" needs a fact
    that says "deliver...". Multi-word phrases must appear whole; "led" is matched whole.
    """
    line_tokens = set(_tokenize(text))
    grounded = set(grounded) | {_SYNONYMS.get(token, token) for token in grounded}
    for stem in _CLAIM_PREFIXES:
        matched = next((token for token in sorted(line_tokens) if token.startswith(stem)), None)
        if matched and not any(token.startswith(stem) for token in grounded):
            return f"claim not supported by cited facts: '{matched}'"
    for term in sorted(_CLAIM_EXACT):
        if term in line_tokens and term not in grounded:
            return f"claim not supported by cited facts: '{term}'"
    for phrase in _CLAIM_PHRASES:
        phrase_tokens = set(_tokenize(phrase))
        if phrase_tokens and phrase_tokens <= line_tokens and not phrase_tokens <= grounded:
            return f"claim not supported by cited facts: '{phrase}'"
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
        if is_bullet and line.source != PROFILE_VERBATIM and not _starts_with_action_verb(text):
            return False, "bullet does not start with an action verb"
        flagged = _ungrounded(text, allowed, tech_tokens, self.ontology)
        if flagged:
            return False, f"term(s) not in cited facts: {', '.join(flagged)}"
        claim = _claim_error(text, allowed | tech_tokens)
        if claim:
            return False, claim
        ungrounded = _ungrounded_content(text, allowed | tech_tokens)
        if len(ungrounded) > 1:
            return False, f"content words not in cited facts: {', '.join(ungrounded)}"
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
    """Flatten a resume into the text blocks an ATS would read.

    ``additional_tools`` is deliberately absent: those skills are shown on the review
    page and only exported when the owner selects them.
    """
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
            if _fact_mentions(fact.text, concept):
                return fact
            tech_match = tech_match or fact
    if tech_match is not None:
        return tech_match
    for fact in facts.values():
        if _fact_mentions(fact.text, concept):
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


def _requirement_named_in(name: str, text: str) -> bool:
    """Whether the requirement's own meaningful words appear in a cited text.

    A requirement met only through an adjacent technology ("security awareness" via a
    fact that mentions JWT) is not *named*, so a free-text "covered" judgement for it
    degrades to "partial" (item 7). A requirement made of generic nouns only is treated
    as named, so it is never downgraded this way.
    """
    tokens = {token for token in _tokenize(name) if len(token) > 2 and token not in _GENERIC_TERMS}
    if not tokens:
        return True
    return bool(tokens & set(_tokenize(text)))


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
                        status = draft.status
                        if status == "covered" and not _requirement_named_in(name, cited_text):
                            status = "partial"
                        coverage = RequirementCoverage(
                            requirement=name,
                            status=status,
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
            kept.append(
                ResumeLine(text=line.text.strip(), facts=list(line.facts), source=line.source)
            )
        else:
            logger.warning("tailored-resume %s dropped: %s", label, reason)
            dropped.append(
                DroppedLine(text=(line.text or "").strip(), reason=reason, section=label)
            )
    return kept


def _draft_errors(draft: ResumeDraft) -> list[str]:
    errors: list[str] = []
    if len(draft.summary) > SUMMARY_MAX:
        errors.append(f"summary must have at most {SUMMARY_MAX} sentences")
    return errors


def _join_and(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _code_summary(profile: CandidateProfile, skills: list[str]) -> ResumeLine:
    """The summary's first sentence, assembled in code from profile fields + top skills.

    No model sentence can state the candidate's level/experience/skills more reliably, and
    a project sentence must never stand in for the summary.
    """
    level = getattr(profile.experience_level, "value", profile.experience_level)
    level = str(level or "fresher").strip().capitalize()
    role = (profile.target_roles[0] if profile.target_roles else "").strip().lower()
    if not role:
        role = "software"
    if "developer" not in role and "engineer" not in role:
        role = f"{role} developer"
    internship_months = sum(entry.duration_months or 0 for entry in profile.internships)
    months = internship_months or profile.total_experience_months or 0
    top = [name for name in skills[:3] if name]
    scope = ("internship experience" if internship_months else "experience") if months else ""
    if top and months:
        text = f"{level} {role} with {months} months of {scope} in {_join_and(top)}."
    elif top:
        text = f"{level} {role} with experience in {_join_and(top)}."
    else:
        text = f"{level} {role} with hands-on experience."
    return ResumeLine(text=text, facts=[])


def _cites_only_projects(line: ResumeLine, facts: dict[str, Fact]) -> bool:
    """True when every cited fact belongs to a project (so it cannot be the summary)."""
    ids = [fact_id for fact_id in line.facts if fact_id in facts]
    return bool(ids) and all(fact_id.startswith("P") for fact_id in ids)


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
    skills, familiar, additional = classify_skills(profile, parsed_jd, ontology)
    skill_facts = skill_evidence_facts(profile, ontology)
    grounder.seed_skill_keywords([*skills, *familiar])

    dropped: list[DroppedLine] = []
    code_line = _code_summary(profile, skills)
    grounder.counts.update(_keyword_counts(code_line.text, grounder.jd_tokens))

    profile_experience = [*profile.internships, *profile.work]
    experience: list[ExperienceEntry] = []
    for index, entry in enumerate(profile_experience):
        bullets = draft.experience[index].bullets if index < len(draft.experience) else []
        experience.append(
            ExperienceEntry(
                role=entry.role,
                company=entry.company,
                duration_months=entry.duration_months,
                dates=format_dates(entry.dates),
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
                tech=clean_tech_items(project.tech),
                bullets=_keep_lines(
                    bullets,
                    grounder,
                    f"projects[{index}]",
                    is_bullet=True,
                    dropped=dropped,
                ),
            )
        )

    # The summary's second sentence is kept only when it does not just repeat a bullet.
    bullet_texts = [b.text for entry in experience for b in entry.bullets]
    bullet_texts += [b.text for project in projects for b in project.bullets]
    model_summary = _keep_lines(
        draft.summary, grounder, "summary", is_bullet=False, dropped=dropped
    )
    model_summary = [
        line
        for line in model_summary
        if not _cites_only_projects(line, grounder.facts)
        and _overlap_ratio(line.text, bullet_texts) < SUMMARY_OVERLAP_MAX
    ]
    summary = [code_line, *model_summary[: SUMMARY_MAX - 1]]

    requirements = _classify_requirements(
        profile, parsed_jd, ontology, grounder.facts, draft.requirements
    )
    missing = [item.requirement for item in requirements if item.status == "missing"]

    resume = TailoredResume(
        header=ResumeHeader(name=profile.full_name, email=profile.email, links=profile.links),
        summary=summary,
        skills=skills,
        familiar_skills=familiar,
        additional_tools=additional,
        skill_facts=skill_facts,
        experience=experience,
        projects=projects,
        education=[edu.model_dump() for edu in profile.education],
        certifications=list(profile.certifications),
        requirements=requirements,
        missing_requirements=missing,
        dropped=dropped,
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


def _internship_indices(profile: CandidateProfile) -> list[int]:
    """The ``experience`` indices that hold internships (they come before work entries)."""
    return list(range(len(profile.internships)))


def _internship_bullet_count(resume: TailoredResume, profile: CandidateProfile) -> int:
    return sum(
        len(resume.experience[index].bullets)
        for index in _internship_indices(profile)
        if index < len(resume.experience)
    )


def _internship_is_thin(resume: TailoredResume, profile: CandidateProfile) -> bool:
    """The internship section is thin when it has fewer than the minimum grounded bullets."""
    return (
        bool(profile.internships)
        and _internship_bullet_count(resume, profile) < MIN_INTERNSHIP_BULLETS
    )


def _empty_project_indices(resume: TailoredResume) -> list[int]:
    return [index for index, project in enumerate(resume.projects) if not project.bullets]


def _project_fact_ids(index: int, facts: dict[str, Fact]) -> list[str]:
    prefix = f"P{index + 1}."
    return [fact_id for fact_id in facts if fact_id.startswith(prefix)]


def _internship_fact_ids(profile: CandidateProfile, facts: dict[str, Fact]) -> list[str]:
    ids: list[str] = []
    for index in _internship_indices(profile):
        prefix = f"I{index + 1}."
        ids.extend(fact_id for fact_id in facts if fact_id.startswith(prefix))
    return ids


def _drop_lines_for(resume: TailoredResume, sections: set[str]) -> list[str]:
    return [f"- {line.text}: {line.reason}" for line in resume.dropped if line.section in sections]


def _project_repair_user(
    base_user: str,
    index: int,
    profile: CandidateProfile,
    resume: TailoredResume,
    facts: dict[str, Fact],
) -> str:
    """A repair prompt for one empty project: names it, lists its sentence ids and drops."""
    project = profile.projects[index]
    title = project.title or f"project {index + 1}"
    sentence_ids = _project_fact_ids(index, facts)
    drops = _drop_lines_for(resume, {f"projects[{index}]"})
    return "\n".join(
        [
            base_user,
            "",
            "## Repair pass (one project)",
            f"Project {index + 1} ({title!r}) ended with no grounded bullets.",
            "Its citable sentence ids are: " + (", ".join(sentence_ids) or "(none)") + ".",
            "Lines that already failed validation for this project (do not repeat them):",
            *(drops or ["- (none)"]),
            (
                "Write 1-3 grounded bullets for THIS project only, condensing or rewording its "
                "own sentences and adding nothing. Avoid any line listed above."
            ),
            (
                'Return ONLY the "projects" key: a single-element array whose one entry holds '
                "the bullets. Leave it empty if you cannot ground a bullet. Cite sentence ids "
                f"such as [{sentence_ids[0] if sentence_ids else 'P' + str(index + 1) + '.summary.s1'}]."
            ),
        ]
    )


def _internship_repair_user(
    base_user: str,
    profile: CandidateProfile,
    resume: TailoredResume,
    facts: dict[str, Fact],
) -> str:
    """A repair prompt for the thin internship section: names it, lists ids and drops."""
    names = [f"{entry.role or '?'} @ {entry.company or '?'}" for entry in profile.internships]
    sentence_ids = _internship_fact_ids(profile, facts)
    sections = {f"experience[{index}]" for index in _internship_indices(profile)}
    drops = _drop_lines_for(resume, sections)
    return "\n".join(
        [
            base_user,
            "",
            "## Repair pass (internship)",
            "The internship section ("
            + "; ".join(names)
            + ") has fewer than "
            + str(MIN_INTERNSHIP_BULLETS)
            + " grounded bullets.",
            "Its citable sentence ids are: " + (", ".join(sentence_ids) or "(none)") + ".",
            "Lines that already failed validation for these entries (do not repeat them):",
            *(drops or ["- (none)"]),
            (
                "Write one grounded bullet per internship sentence until there are at least "
                f"{MIN_INTERNSHIP_BULLETS} in total; condense or reword only, add nothing."
            ),
            (
                'Return ONLY the "experience" key, an array in the SAME order as the candidate '
                "facts: fill the internship entries, leave every other entry's bullets empty."
            ),
        ]
    )


def _merge_project_repair(draft: ResumeDraft, index: int, repair: ResumeRepair) -> None:
    """Copy a repaired project's bullets into the draft (accept a single-entry reply too)."""
    while len(draft.projects) <= index:
        draft.projects.append(DraftEntry())
    entry = repair.projects[index] if index < len(repair.projects) else None
    if entry is None or not entry.bullets:
        entry = repair.projects[0] if repair.projects else None
    if entry is not None and entry.bullets:
        draft.projects[index] = entry


def _merge_internship_repair(
    draft: ResumeDraft, profile: CandidateProfile, repair: ResumeRepair
) -> None:
    """Copy repaired internship bullets into the draft, aligned to the experience order."""
    indices = _internship_indices(profile)
    while len(draft.experience) <= max(indices, default=-1):
        draft.experience.append(DraftEntry())
    for index in indices:
        if index < len(repair.experience) and repair.experience[index].bullets:
            draft.experience[index] = repair.experience[index]


def _verbatim_line(fact_id: str, facts: dict[str, Fact]) -> ResumeLine | None:
    fact = facts.get(fact_id)
    if fact is None or not fact.text.strip():
        return None
    return ResumeLine(text=fact.text, facts=[fact_id], source=PROFILE_VERBATIM)


def _project_verbatim_line(index: int, facts: dict[str, Fact]) -> ResumeLine | None:
    """The project's own summary (preferred) or outcome sentence, taken verbatim."""
    key = f"P{index + 1}"
    for prefix in (f"{key}.summary.s", f"{key}.outcomes.s"):
        ids = sorted(
            (fact_id for fact_id in facts if fact_id.startswith(prefix)),
            key=lambda fact_id: int(fact_id.rsplit(".s", 1)[1]),
        )
        for fact_id in ids:
            line = _verbatim_line(fact_id, facts)
            if line is not None:
                return line
    return None


def _fill_internship_verbatim(
    resume: TailoredResume, profile: CandidateProfile, facts: dict[str, Fact]
) -> list[str]:
    """Top the internship section up to the minimum with verbatim profile sentences."""
    notes: list[str] = []
    indices = _internship_indices(profile)
    used = {
        fact_id
        for index in indices
        if index < len(resume.experience)
        for bullet in resume.experience[index].bullets
        for fact_id in bullet.facts
    }
    for index in indices:
        if index >= len(resume.experience):
            continue
        entry = profile.internships[index]
        for position, sentence in enumerate(
            _split_sentences(" ".join(entry.description or [])), start=1
        ):
            if _internship_bullet_count(resume, profile) >= MIN_INTERNSHIP_BULLETS:
                return notes
            fact_id = f"I{index + 1}.s{position}"
            if fact_id in used or fact_id not in facts:
                continue
            line = _verbatim_line(fact_id, facts)
            if line is None:
                continue
            resume.experience[index].bullets.append(line)
            used.add(fact_id)
            notes.append(
                f"internship '{entry.role or '?'} @ {entry.company or '?'}': fewer than "
                f"{MIN_INTERNSHIP_BULLETS} grounded bullets; used a profile-verbatim sentence"
            )
    return notes


def generate_tailored_resume(
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
) -> TailoredResume:
    """Draft and validate a tailored resume for ``job`` from ``profile``.

    Every LLM call (the draft retries and the per-section repairs) draws from the single
    :data:`MAX_RESUME_CALLS` budget, so cost is bounded. A project left empty by the
    model gets one repair call of its own (up to :data:`MAX_SECTION_REPAIR_ATTEMPTS`
    attempts), and the internship section likewise when it is thin; anything still
    missing falls back in code to a verbatim profile sentence and is warned about.
    """
    settings = settings or get_settings()
    ontology = ontology or get_ontology()
    system_prompt, version = load_prompt("tailored_resume")
    model = settings.models.evaluator

    gaps = profile_gaps(profile)
    if gaps:
        logger.warning("tailored-resume refused: %s", "; ".join(gaps))
        return _refusal(profile, version, model, gaps)

    base_user = build_user_prompt(profile, job, parsed_jd)
    facts = build_facts(profile)
    logger.info("Tailored resume: drafting for '%s' @ '%s'", job.title, job.company)
    calls = 0
    draft: ResumeDraft | None = None
    errors: list[str] = []
    for attempt in range(MAX_RESUME_RETRIES + 1):
        if calls >= MAX_RESUME_CALLS:
            break
        user = base_user if attempt == 0 else f"{base_user}\n\n{_retry_message(errors)}"
        draft = client.call_json(
            schema=ResumeDraft,
            system=system_prompt,
            user=user,
            model=model,
            temperature=0.0,
        )
        calls += 1
        errors = _draft_errors(draft)
        if not errors:
            break
        logger.warning(
            "tailored-resume draft failed validation (attempt %s/%s): %s",
            attempt + 1,
            MAX_RESUME_RETRIES + 1,
            errors,
        )
    draft = cast(ResumeDraft, draft if draft is not None else ResumeDraft())

    def assemble() -> TailoredResume:
        return _assemble(
            draft,
            profile=profile,
            parsed_jd=parsed_jd,
            version=version,
            model=model,
            ontology=ontology,
        )

    resume = assemble()

    # One repair call per empty project, then the internship section if it is thin,
    # all within the single per-resume call budget.
    for index in _empty_project_indices(resume):
        for _attempt in range(MAX_SECTION_REPAIR_ATTEMPTS):
            if calls >= MAX_RESUME_CALLS:
                break
            logger.info("tailored-resume repair: empty project %s", index + 1)
            repair = client.call_json(
                schema=ResumeRepair,
                system=system_prompt,
                user=_project_repair_user(base_user, index, profile, resume, facts),
                model=model,
                temperature=0.0,
            )
            calls += 1
            _merge_project_repair(draft, index, cast(ResumeRepair, repair))
            resume = assemble()
            if resume.projects[index].bullets:
                break

    for _attempt in range(MAX_SECTION_REPAIR_ATTEMPTS):
        if calls >= MAX_RESUME_CALLS or not _internship_is_thin(resume, profile):
            break
        logger.info("tailored-resume repair: thin internship section")
        repair = client.call_json(
            schema=ResumeRepair,
            system=system_prompt,
            user=_internship_repair_user(base_user, profile, resume, facts),
            model=model,
            temperature=0.0,
        )
        calls += 1
        _merge_internship_repair(draft, profile, cast(ResumeRepair, repair))
        resume = assemble()

    # Code fallback: a still-empty project gets its own summary/outcome sentence, and a
    # still-thin internship gets its own description sentences, both verbatim.
    fallback_notes: list[str] = []
    for index, project in enumerate(resume.projects):
        if project.bullets:
            continue
        line = _project_verbatim_line(index, facts)
        if line is not None:
            project.bullets = [line]
            fallback_notes.append(
                f"project '{project.title or index + 1}': no grounded bullets; used a "
                "profile-verbatim sentence"
            )
    if _internship_is_thin(resume, profile):
        fallback_notes.extend(_fill_internship_verbatim(resume, profile, facts))

    resume.warnings = _resume_warnings(resume, profile, fallback_notes)
    resume.ats = ats_lint(resume)
    return resume


def _resume_warnings(
    resume: TailoredResume, profile: CandidateProfile, fallback_notes: list[str]
) -> list[str]:
    """Owner-facing warnings: any thin/empty section and every verbatim fallback used."""
    warnings: list[str] = []
    if _internship_is_thin(resume, profile):
        warnings.append("internship section thin: add detail on the Profile page")
    for index, project in enumerate(resume.projects):
        if not project.bullets:
            warnings.append(f"project '{project.title or index + 1}' has no grounded bullets")
    warnings.extend(fallback_notes)
    return warnings


def generate_tailored_resume_under_caps(
    conn: Any,
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    parsed_jd: ParsedJD,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
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
            budgeted,
            profile=profile,
            job=job,
            parsed_jd=parsed_jd,
            settings=settings,
            ontology=ontology,
        )
    finally:
        delta = int(getattr(budgeted, "tokens_used", 0) or 0) - start_tokens
        if delta > 0:
            add_llm_tokens(conn, delta)


@dataclass
class ResumeGeneration:
    """One on-demand tailored-resume build and what it cost."""

    resume: TailoredResume
    tokens: int
    calls: int
    cached: bool


def profile_hash(profile: CandidateProfile) -> str:
    """A stable hash of the profile, so a repeat build can be recognised."""
    return hashlib.sha256(profile.model_dump_json().encode("utf-8")).hexdigest()


def generate_tailored_resume_cached(
    conn: Any,
    client: LLMClient,
    *,
    profile: CandidateProfile,
    job: Job,
    job_id: int,
    parsed_jd: ParsedJD,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
) -> ResumeGeneration:
    """Build on demand, caching by ``(profile hash, job id, prompt version)``.

    A repeat build for the same profile, job and prompt version returns the stored
    resume with **zero** LLM calls; otherwise it generates under the caps, stores the
    result and reports the tokens/calls spent. Never called from a bulk run.
    """
    settings = settings or get_settings()
    ontology = ontology or get_ontology()
    _system, version = load_prompt("tailored_resume")
    digest = profile_hash(profile)

    cached = find_cached_tailored_resume(conn, job_id, digest, version)
    if cached is not None:
        return ResumeGeneration(
            resume=TailoredResume.model_validate_json(cached["content_json"]),
            tokens=0,
            calls=0,
            cached=True,
        )

    start_tokens = int(getattr(client, "tokens_used", 0) or 0)
    start_calls = int(getattr(client, "calls", 0) or 0)
    resume = generate_tailored_resume_under_caps(
        conn,
        client,
        profile=profile,
        job=job,
        parsed_jd=parsed_jd,
        settings=settings,
        ontology=ontology,
    )
    tokens = int(getattr(client, "tokens_used", 0) or 0) - start_tokens
    calls = int(getattr(client, "calls", 0) or 0) - start_calls
    save_tailored_resume(
        conn,
        job_id,
        content_json=resume.model_dump_json(),
        model=resume.model,
        prompt_version=version,
        profile_hash=digest,
    )
    return ResumeGeneration(resume=resume, tokens=tokens, calls=calls, cached=False)
