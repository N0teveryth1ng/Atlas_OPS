"""JD Parser Agent — regex pre-pass + LLM, merged conservatively.

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

logger = logging.getLogger(__name__)

_NUM = r"\d+(?:\.\d+)?"

_RANGE_RE = re.compile(rf"({_NUM})\s*(?:-|–|—|to)\s*({_NUM})\s*\+?\s*(?:years?|yrs?)\b", re.I)
_PLUS_RE = re.compile(rf"({_NUM})\s*\+\s*(?:years?|yrs?)\b", re.I)
_MIN_RE = re.compile(
    rf"(?:minimum(?:\s+of)?|at\s+least|min\.?|over|more\s+than)\s*({_NUM})\s*(?:years?|yrs?)",
    re.I,
)
_YEARS_OF_RE = re.compile(
    rf"({_NUM})\s*(?:years?|yrs?)\s+of\s+(?:relevant\s+|professional\s+|industry\s+)?experience",
    re.I,
)
_FRESHER_RE = re.compile(
    r"\b(fresher|fresh(?:er)?\s+graduate|entry[\s-]?level|no\s+(?:prior\s+)?experience|"
    r"recent\s+graduate|graduate\s+trainee|trainee|internship|0\s*[-–]\s*1\s*years?)\b",
    re.I,
)

_RED_FLAG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(10|1[1-9]|[2-9]\d)\s*\+?\s*(?:years?|yrs?)\b", re.I), "very_high_experience"),
    (re.compile(r"\bteam\s+lead\b|\blead\s+a\s+team\b|\bmentor\s+juniors?\b", re.I), "leadership_expected"),
    (re.compile(r"\bon-?call\s+24|24\s*x\s*7|24/7\b", re.I), "always_on_call"),
    (re.compile(r"\bunpaid\b|\bwithout\s+compensation\b", re.I), "unpaid"),
    (
        re.compile(r"\bown\s+the\s+architecture\b|\bset\s+the\s+technical\s+direction\b", re.I),
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
        candidates.append((match.start(), match.end(), float(match.group(1)), None, match.group(0), False))
    for match in _MIN_RE.finditer(text):
        candidates.append((match.start(), match.end(), float(match.group(1)), None, match.group(0), False))
    for match in _YEARS_OF_RE.finditer(text):
        candidates.append((match.start(), match.end(), float(match.group(1)), None, match.group(0), False))

    if not candidates:
        fresher = _FRESHER_RE.search(text)
        if fresher:
            return 0.0, None, fresher.group(0)
        return None, None, None

    # A standalone "X years of experience" that begins inside an "A-B years"
    # range is the range's upper bound, not a separate requirement: drop it.
    ranges = [c for c in candidates if c[5]]
    kept = [
        c for c in candidates if c[5] or not any(r[0] <= c[0] < r[1] for r in ranges)
    ]
    kept.sort(key=lambda item: item[2], reverse=True)
    best = kept[0]
    return best[2], best[3], best[4]


def detect_title_seniority(title: str) -> Seniority:
    """Most senior level hinted by the title (conservative on ambiguity)."""
    lowered = (title or "").lower()
    best = Seniority.unknown
    for pattern, level in _TITLE_RULES:
        if re.search(rf"(?<![a-z]){re.escape(pattern)}(?![a-z])", lowered):
            if SENIORITY_RANK[level] > SENIORITY_RANK[best]:
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


def merge_jd(regex_jd: ParsedJD, llm_jd: ParsedJD) -> ParsedJD:
    """Merge regex and LLM parses, always taking the more conservative value."""
    merged = llm_jd.model_copy(deep=True)

    mins = [y for y in (regex_jd.min_years_experience, llm_jd.min_years_experience) if y is not None]
    merged.min_years_experience = max(mins) if mins else None
    if merged.max_years_experience is None:
        merged.max_years_experience = regex_jd.max_years_experience

    merged.title_seniority = _more_senior(regex_jd.title_seniority, llm_jd.title_seniority)
    merged.years_source_quote = regex_jd.years_source_quote or llm_jd.years_source_quote
    if regex_jd.remote_type != RemoteType.unknown:
        merged.remote_type = regex_jd.remote_type
    merged.red_flags = sorted(set(regex_jd.red_flags) | set(llm_jd.red_flags))

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
    merged = merge_jd(regex_jd, llm_jd)
    merged.title = title or merged.title
    return merged
