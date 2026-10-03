"""Skill ontology + layered matching (plan section 5.7).

Replaces naive set intersection with:

1. exact canonical-name match,
2. alias match (``ReactJS`` -> ``react``, ``psql`` -> ``postgresql``),
3. embedding similarity fallback (sentence-transformers if installed, else a
   deterministic fuzzy-string fallback so the pipeline never hard-depends on it),
4. related-skill partial credit (``Django`` credits the ``Python`` requirement).

Output is a weighted ``must_have_coverage`` hard metric, plus nice-to-have
coverage and the list of unmet must-have skills.
"""

from __future__ import annotations

import difflib
import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .config import REPO_ROOT
from .schemas import Proficiency, Skill

logger = logging.getLogger(__name__)

SKILLS_PATH = REPO_ROOT / "skills.yaml"
EMBEDDING_THRESHOLD = 0.83
RELATED_CREDIT = 0.5
EMBEDDING_CREDIT = 0.4
PROFICIENCY_WEIGHT: dict[Proficiency, float] = {
    Proficiency.strong: 1.0,
    Proficiency.working: 0.7,
    Proficiency.familiar: 0.4,
}

_STRIP = {" ", ".", "-", "_", "/"}


def _key(name: str) -> str:
    """Normalise a skill name for matching (keeps ``+`` and ``#``)."""
    return "".join(ch for ch in (name or "").lower().strip() if ch not in _STRIP)


@dataclass
class OntologyEntry:
    canonical: str
    category: str = "other"
    aliases: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class SkillOntology:
    entries: dict[str, OntologyEntry] = field(default_factory=dict)
    alias_to_canonical: dict[str, str] = field(default_factory=dict)

    def canonicalize(self, name: str) -> str:
        key = _key(name)
        return self.alias_to_canonical.get(key, key)

    def entry(self, canonical: str) -> OntologyEntry | None:
        return self.entries.get(canonical)

    def related(self, canonical: str) -> set[str]:
        entry = self.entries.get(canonical)
        return set(entry.related) if entry else set()


def load_ontology(path: Path | str = SKILLS_PATH) -> SkillOntology:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    ontology = SkillOntology()
    for canonical, data in (raw.get("skills") or {}).items():
        data = data or {}
        entry = OntologyEntry(
            canonical=canonical,
            category=data.get("category", "other"),
            aliases=list(data.get("aliases") or []),
            related=list(data.get("related") or []),
        )
        ontology.entries[canonical] = entry
        ontology.alias_to_canonical[_key(canonical)] = canonical
        for alias in entry.aliases:
            ontology.alias_to_canonical[_key(alias)] = canonical
    return ontology


@lru_cache(maxsize=1)
def get_ontology() -> SkillOntology:
    return load_ontology()


def make_embedder(model_name: str = "all-MiniLM-L6-v2"):
    """Return an ``embed(list[str]) -> list[list[float]]`` callable, or None."""
    try:
        from sentence_transformers import SentenceTransformer
    except Exception:  # pragma: no cover - import depends on optional install
        logger.info("sentence-transformers not installed; using fuzzy fallback")
        return None
    model = SentenceTransformer(model_name)

    def embed(texts: list[str]) -> list[list[float]]:
        return model.encode(list(texts), normalize_embeddings=True).tolist()

    return embed


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def _similarity(jd_skill: str, profile_name: str, embedder, cache: dict) -> float:
    if embedder is None:
        return difflib.SequenceMatcher(None, _key(jd_skill), _key(profile_name)).ratio()
    cache_key = (jd_skill, profile_name)
    if cache_key not in cache:
        vectors = embedder([jd_skill, profile_name])
        cache[cache_key] = _cosine(vectors[0], vectors[1])
    return cache[cache_key]


@dataclass
class SkillMatch:
    must_have_coverage: float
    nice_to_have_coverage: float
    missing_must_haves: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)


def _credit(
    jd_skill: str,
    profile_by_canonical: dict[str, Skill],
    profile_skills: list[Skill],
    ontology: SkillOntology,
    embedder,
    cache: dict,
) -> float:
    canonical = ontology.canonicalize(jd_skill)

    if canonical in profile_by_canonical:
        return PROFICIENCY_WEIGHT.get(profile_by_canonical[canonical].proficiency, 0.4)

    if ontology.related(canonical) & set(profile_by_canonical):
        return RELATED_CREDIT
    if any(canonical in ontology.related(p.canonical_name) for p in profile_skills):
        return RELATED_CREDIT

    best = max(
        (_similarity(jd_skill, p.name, embedder, cache) for p in profile_skills),
        default=0.0,
    )
    return EMBEDDING_CREDIT if best >= EMBEDDING_THRESHOLD else 0.0


def match_skills(
    must_have: list[str],
    nice_to_have: list[str],
    profile_skills: list[Skill],
    *,
    ontology: SkillOntology | None = None,
    embedder=None,
) -> SkillMatch:
    ontology = ontology or get_ontology()
    profile_by_canonical = {s.canonical_name: s for s in profile_skills}
    cache: dict = {}

    must_credits = [
        (_credit(s, profile_by_canonical, profile_skills, ontology, embedder, cache), s)
        for s in must_have
    ]
    nice_credits = [
        (_credit(s, profile_by_canonical, profile_skills, ontology, embedder, cache), s)
        for s in nice_to_have
    ]

    must_cov = sum(c for c, _ in must_credits) / len(must_credits) if must_credits else 1.0
    nice_cov = sum(c for c, _ in nice_credits) / len(nice_credits) if nice_credits else 1.0
    missing = [s for c, s in must_credits if c == 0.0]
    matched = [s for c, s in must_credits + nice_credits if c > 0.0]

    return SkillMatch(
        must_have_coverage=round(must_cov, 4),
        nice_to_have_coverage=round(nice_cov, 4),
        missing_must_haves=missing,
        matched=matched,
    )
