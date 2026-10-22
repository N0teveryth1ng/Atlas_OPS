"""JD Parser Agent - regex pre-pass + LLM, merged conservatively.

Section 5.5 of the rework plan. The deterministic regex pass extracts year
requirements and title seniority first. The LLM adds must-have / nice-to-have
skills and context. When the two disagree, the *more conservative* value wins
and the job is flagged, so a low-precision parse never over-promotes a role.
"""

from __future__ import annotations

import logging
import re

from .llm import LLMClient
from .prompt_store import load_prompt
from .schemas import ParsedJD, RemoteType, Seniority
from .skills import get_ontology

logger = logging.getLogger(__name__)

_NUM = r"\d+(?:\.\d+)?"

_RANGE_RE = re.compile(
    rf"({_NUM})\s*(?:-|-|-|to)\s*({_NUM})\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
)
_PLUS_RE = re.compile(rf"({_NUM})\s*\+\s*(?:years?|yrs?)\b", re.IGNORECASE)
_MIN_RE = re.compile(
    rf"(?:minimum(?:\s+of)?|at\s+least|min\.?|over|more\s+than)\s*({_NUM})\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_YEARS_OF_RE = re.compile(
    rf"({_NUM})\s*(?:years?|yrs?)\s+of\s+(?:relevant\s+|professional\s+|industry\s+)?experience",
    re.IGNORECASE,
)
# A bare "N years" / "N yrs" (optionally "N years experience"). Many postings
# state the requirement without "of experience"; missing these let an
# over-qualified role slip through the hard filter.
_BARE_YEARS_RE = re.compile(rf"({_NUM})\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
# Reject "N years" when it clearly is not a requirement ("over the last 3 years",
# "founded 5 years ago").
_NON_REQ_BEFORE_RE = re.compile(
    r"(?:last|past|next|previous|over|nearly|almost|after|before|every|per|around|about|"
    r"for\s+the|since)\s+(?:the\s+)?$",
    re.IGNORECASE,
)
_NON_REQ_AFTER_RE = re.compile(
    r"^\s*(?:ago|old|of\s+age|in\s+business|running|strong|now)\b",
    re.IGNORECASE,
)
_FRESHER_RE = re.compile(
    r"\b(fresher|fresh(?:er)?\s+graduate|entry[\s-]?level|no\s+(?:prior\s+)?experience|"
    r"recent\s+graduate|graduate\s+trainee|trainee|internship|0\s*[--]\s*1\s*years?)\b",
    re.IGNORECASE,
)

_RED_FLAG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\b(10|1[1-9]|[2-9]\d)\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE),
        "very_high_experience",
    ),
    (
        re.compile(r"\bteam\s+lead\b|\blead\s+a\s+team\b|\bmentor\s+juniors?\b", re.IGNORECASE),
        "leadership_expected",
    ),
    (re.compile(r"\bon-?call\s+24|24\s*x\s*7|24/7\b", re.IGNORECASE), "always_on_call"),
    (re.compile(r"\bunpaid\b|\bwithout\s+compensation\b", re.IGNORECASE), "unpaid"),
    (
        re.compile(
            r"\bown\s+the\s+architecture\b|\bset\s+the\s+technical\s+direction\b", re.IGNORECASE
        ),
        "ownership_of_architecture",
    ),
]

_TITLE_RULES: list[tuple[str, Seniority]] = [
    ("internship", Seniority.intern),
    ("intern", Seniority.intern),
    ("entry level", Seniority.entry),
    ("entry-level", Seniority.entry),
    ("graduate", Seniority.entry),
    ("trainee", Seniority.entry),
    ("junior", Seniority.junior),
    ("jr", Seniority.junior),
    ("associate", Seniority.junior),
    ("senior", Seniority.senior),
    ("sr", Seniority.senior),
    ("team lead", Seniority.lead),
    ("lead", Seniority.lead),
    ("staff", Seniority.principal),
    ("principal", Seniority.principal),
    ("architect", Seniority.architect),
    ("manager", Seniority.manager),
    ("head of", Seniority.manager),
    ("director", Seniority.manager),
    ("vice president", Seniority.manager),
    ("vp", Seniority.manager),
]

SENIORITY_RANK: dict[Seniority, int] = {
    Seniority.unknown: -1,
    Seniority.intern: 0,
    Seniority.entry: 1,
    Seniority.junior: 2,
    Seniority.mid: 3,
    Seniority.senior: 4,
    Seniority.lead: 5,
    Seniority.principal: 6,
    Seniority.architect: 7,
    Seniority.manager: 7,
}

_VAGUE_CONCEPTS = {
    "ai/ml/nlp concepts",
    "ai/ml concepts",
    "ml/nlp concepts",
    "nlp concepts",
    "machine learning concepts",
    "cloud infrastructure",
    "data structures",
    "system design patterns",
    "software design patterns",
    "design patterns",
    "apis",
    "api",
    "apis in general",
    "general apis",
}

_SPOKEN_LANGUAGES = {
    "english",
    "spanish",
    "french",
    "german",
    "hindi",
    "mandarin",
    "chinese",
    "japanese",
    "korean",
    "arabic",
    "portuguese",
    "italian",
    "russian",
}

_DEGREE_WORDS = (
    "bachelor",
    "master",
    "phd",
    "b.tech",
    "m.tech",
    "btech",
    "mtech",
    "b.e",
    "m.e",
    "bsc",
    "msc",
)


def _norm_skill(s: str) -> str:
    return " ".join((s or "").lower().strip().split())


def _looks_like_named_tool(skill: str, job_text: str) -> bool:
    s = (skill or "").strip()
    if not s:
        return False
    if any(ch.isupper() for ch in s) and len(s) >= 2:
        return True
    if "+" in s or "#" in s:
        return True
    if "/" in s and re.search(r"[A-Za-z]+/[A-Za-z]+", s):
        return True
    return bool(job_text and s.lower() in job_text.lower() and s.isupper() and len(s) >= 2)


def _reclassify_skills(parsed: ParsedJD, job_text: str) -> tuple[list[str], list[str], list[str]]:
    ontology = get_ontology()
    must: list[str] = []
    nice: list[str] = []
    dropped: list[str] = []
    for skill in parsed.must_have_skills or []:
        key = _norm_skill(skill)
        if key in _SPOKEN_LANGUAGES or any(d in key for d in _DEGREE_WORDS):
            dropped.append(skill)
            logger.debug("jd_parser: dropped must-have '%s' (spoken language/degree)", skill)
            continue
        if key in _VAGUE_CONCEPTS:
            nice.append(skill)
            logger.debug("jd_parser: moved must-have '%s' to nice-to-have (vague concept)", skill)
            continue
        canonical = ontology.canonicalize(skill)
        if canonical in ontology.entries:
            must.append(skill)
            continue
        norm_key = _norm_skill(skill).replace(" ", "").replace("-", "").replace(".", "")
        canon_key = canonical.replace(" ", "").replace("-", "").replace(".", "")
        if canon_key and canon_key != norm_key and canonical in ontology.entries:
            must.append(skill)
            continue
        if _looks_like_named_tool(skill, job_text):
            must.append(skill)
            continue
        if len(skill.split()) >= 3:
            nice.append(skill)
            logger.debug(
                "jd_parser: moved must-have '%s' to nice-to-have (not concrete named tool)", skill
            )
            continue
        nice.append(skill)
        logger.debug("jd_parser: moved must-have '%s' to nice-to-have (conservative)", skill)
    for skill in parsed.nice_to_have_skills or []:
        if skill not in nice and skill not in must:
            nice.append(skill)

    def _dedupe(items: list[str]) -> list[str]:
        seen = set()
        out = []
        for item in items:
            k = _norm_skill(item)
            if k in seen:
                continue
            seen.add(k)
            out.append(item)
        return out

    return _dedupe(must), _dedupe(nice), dropped


def extract_years(text: str) -> tuple[float | None, float | None, str | None]:
    """Return ``(min_years, max_years, source_quote)``.

    When several patterns match, the highest minimum wins (most conservative,
    i.e. most likely to reject a role that asks for more experience).
    """
    text = text or ""
    candidates: list[tuple[int, int, float, float | None, str, bool]] = []

    for match in _RANGE_RE.finditer(text):
        low, high = sorted((float(match.group(1)), float(match.group(2))))
        candidates.append((match.start(), match.end(), low, high, match.group(0), True))
    for match in _PLUS_RE.finditer(text):
        candidates.append(
            (match.start(), match.end(), float(match.group(1)), None, match.group(0), False)
        )
    for match in _MIN_RE.finditer(text):
        candidates.append(
            (match.start(), match.end(), float(match.group(1)), None, match.group(0), False)
        )
    for match in _YEARS_OF_RE.finditer(text):
        candidates.append(
            (match.start(), match.end(), float(match.group(1)), None, match.group(0), False)
        )
    for match in _BARE_YEARS_RE.finditer(text):
        before = text[max(0, match.start() - 24) : match.start()]
        after = text[match.end() : match.end() + 16]
        if _NON_REQ_BEFORE_RE.search(before) or _NON_REQ_AFTER_RE.search(after):
            continue
        candidates.append(
            (match.start(), match.end(), float(match.group(1)), None, match.group(0), False)
        )

    if not candidates:
        fresher = _FRESHER_RE.search(text)
        if fresher:
            return 0.0, None, fresher.group(0)
        return None, None, None

    # A standalone "X years of experience" that begins inside an "A-B years"
    # range is the range's upper bound, not a separate requirement: drop it.
    ranges = [c for c in candidates if c[5]]
    kept = [c for c in candidates if c[5] or not any(r[0] <= c[0] < r[1] for r in ranges)]
    kept.sort(key=lambda item: item[2], reverse=True)
    best = kept[0]
    return best[2], best[3], best[4]


def detect_title_seniority(title: str) -> Seniority:
    """Most senior level hinted by the title (conservative on ambiguity)."""
    lowered = (title or "").lower()
    best = Seniority.unknown
    for pattern, level in _TITLE_RULES:
        if (
            re.search(rf"(?<![a-z]){re.escape(pattern)}(?![a-z])", lowered)
            and SENIORITY_RANK[level] > SENIORITY_RANK[best]
        ):
            best = level
    return best


def detect_remote_type(text: str) -> RemoteType:
    lowered = (text or "").lower()
    if "hybrid" in lowered:
        return RemoteType.hybrid
    if re.search(r"\bremote\b|\bwork from home\b|\bwfh\b|\btelecommut", lowered):
        return RemoteType.remote
    if re.search(r"on-?site|\bin[- ]office\b", lowered):
        return RemoteType.onsite
    return RemoteType.unknown


def detect_red_flags(text: str) -> list[str]:
    lowered = text or ""
    return sorted({name for pattern, name in _RED_FLAG_PATTERNS if pattern.search(lowered)})


def parse_jd_regex_only(title: str, description: str) -> ParsedJD:
    """Deterministic parse used by the eval harness (no network/LLM)."""
    text = f"{title}\n{description}"
    min_years, max_years, quote = extract_years(text)
    confidence = 0.9 if min_years is not None else 0.4
    return ParsedJD(
        title=title or None,
        title_seniority=detect_title_seniority(title),
        min_years_experience=min_years,
        max_years_experience=max_years,
        years_source_quote=quote,
        remote_type=detect_remote_type(text),
        red_flags=detect_red_flags(text),
        confidence=confidence,
    )


def _more_senior(a: Seniority, b: Seniority) -> Seniority:
    return a if SENIORITY_RANK[a] >= SENIORITY_RANK[b] else b


def merge_jd(regex_jd: ParsedJD, llm_jd: ParsedJD, job_text: str = "") -> ParsedJD:
    """Merge regex and LLM parses, always taking the more conservative value."""
    merged = llm_jd.model_copy(deep=True)

    mins = [
        y for y in (regex_jd.min_years_experience, llm_jd.min_years_experience) if y is not None
    ]
    merged.min_years_experience = max(mins) if mins else None
    if merged.max_years_experience is None:
        merged.max_years_experience = regex_jd.max_years_experience

    merged.title_seniority = _more_senior(regex_jd.title_seniority, llm_jd.title_seniority)
    merged.years_source_quote = regex_jd.years_source_quote or llm_jd.years_source_quote
    if regex_jd.remote_type != RemoteType.unknown:
        merged.remote_type = regex_jd.remote_type
    merged.red_flags = sorted(set(regex_jd.red_flags) | set(llm_jd.red_flags))

    text = job_text or f"{merged.title or ''}\n{merged.responsibilities_summary or ''}"
    must, nice, dropped = _reclassify_skills(merged, text)
    merged.must_have_skills = must
    merged.nice_to_have_skills = nice
    if dropped:
        merged.ambiguities = sorted(
            set(list(merged.ambiguities) + [f"dropped_must_have:{d}" for d in dropped])
        )

    ambiguities = list(merged.ambiguities)
    if (
        regex_jd.min_years_experience is not None
        and llm_jd.min_years_experience is not None
        and abs(regex_jd.min_years_experience - llm_jd.min_years_experience) >= 1
    ):
        ambiguities.append("years_disagree")
        merged.confidence = min(merged.confidence, 0.5)
    if (
        regex_jd.title_seniority != Seniority.unknown
        and llm_jd.title_seniority != Seniority.unknown
        and regex_jd.title_seniority != llm_jd.title_seniority
    ):
        ambiguities.append("seniority_disagree")
        merged.confidence = min(merged.confidence, 0.5)
    merged.ambiguities = sorted(set(ambiguities))
    return merged


def parse_jd(client: LLMClient, *, title: str, description: str) -> ParsedJD:
    """Full parse: regex pre-pass + LLM, merged conservatively."""
    regex_jd = parse_jd_regex_only(title, description)
    system, version = load_prompt("jd_parser")
    logger.info("JD Parser: parsing '%s' (prompt v%s)", title, version)
    llm_jd = client.call_json(
        schema=ParsedJD,
        system=system,
        user=f"Title: {title}\n\nDescription:\n{description}",
    )
    job_text = f"{title}\n{description}"
    merged = merge_jd(regex_jd, llm_jd, job_text=job_text)
    merged.title = title or merged.title
    return merged
