"""Golden-set evaluation harness (Phase 2 acceptance).

Runs the deterministic JD parser + hard filters over ``eval/golden_set.jsonl``
and reports the Phase-2 acceptance numbers from the rework plan:

1. *Required-experience pass-through* — how many roles that clearly ask for more
   than ``my_years + tolerance`` (or demand a senior/lead title) survived the
   hard filter. Must be 0.
2. *Year-extraction accuracy* — the regex pre-pass must recover the stated
   minimum experience on >= 95% of the golden set.

The harness deliberately avoids the LLM so it is free, fast and deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT, Settings, get_settings
from .filters import DISALLOWED_SENIORITY, apply_hard_filters
from .jd_parser import parse_jd_regex_only
from .schemas import CandidateProfile, Job, Proficiency, Skill
from .skills import get_ontology, match_skills

GOLDEN_SET_PATH = REPO_ROOT / "eval" / "golden_set.jsonl"
SKILL_CASES_PATH = REPO_ROOT / "eval" / "skill_cases.jsonl"
YEAR_ACCURACY_TARGET = 0.95
SKILL_ACCURACY_TARGET = 0.95


@dataclass
class EvalCase:
    id: str
    title: str
    description: str
    label: str
    reason: str = ""
    company: str | None = None
    location: str | None = None
    expected_min_years: float | None = None
    expected_seniority: str | None = None


def load_golden_set(path: Path | str = GOLDEN_SET_PATH) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(EvalCase(**json.loads(line)))
    return cases


@dataclass
class EvalReport:
    total: int = 0
    year_checked: int = 0
    year_correct: int = 0
    experience_pass_through: list[str] = field(default_factory=list)
    false_rejects: list[str] = field(default_factory=list)
    label_inconsistencies: list[str] = field(default_factory=list)

    @property
    def year_accuracy(self) -> float:
        return self.year_correct / self.year_checked if self.year_checked else 1.0

    @property
    def accepted(self) -> bool:
        return (
            not self.experience_pass_through
            and not self.false_rejects
            and self.year_accuracy >= YEAR_ACCURACY_TARGET
        )


def evaluate(cases: list[EvalCase] | None = None, *, settings: Settings | None = None) -> EvalReport:
    settings = settings or get_settings()
    cases = cases if cases is not None else load_golden_set()
    allowed = settings.candidate.my_years_experience + settings.candidate.experience_tolerance
    profile = CandidateProfile(total_experience_months=int(settings.candidate.my_years_experience * 12))
    disallowed = {s.value for s in DISALLOWED_SENIORITY}

    report = EvalReport(total=len(cases))
    for case in cases:
        parsed = parse_jd_regex_only(case.title, case.description)
        job = Job(
            source="golden",
            title=case.title,
            company=case.company,
            location=case.location,
            url=f"golden://{case.id}",
            description_raw=case.description,
        )
        result = apply_hard_filters(job, parsed, profile, settings)

        if case.expected_min_years is not None or parsed.min_years_experience is not None:
            report.year_checked += 1
            if parsed.min_years_experience == case.expected_min_years:
                report.year_correct += 1

        should_reject = (
            case.expected_min_years is not None and case.expected_min_years > allowed
        ) or (case.expected_seniority or "") in disallowed

        if case.label == "skip":
            if should_reject and result.passed:
                report.experience_pass_through.append(case.id)
            if not should_reject:
                report.label_inconsistencies.append(case.id)
        elif case.label == "apply" and not result.passed:
            report.false_rejects.append(case.id)

    return report


def format_report(report: EvalReport) -> str:
    lines = [
        f"Golden set cases:            {report.total}",
        f"Year-extraction accuracy:    {report.year_accuracy:.1%} "
        f"({report.year_correct}/{report.year_checked}) target >= {YEAR_ACCURACY_TARGET:.0%}",
        f"Experience pass-throughs:    {len(report.experience_pass_through)} (must be 0) "
        f"{report.experience_pass_through}",
        f"False rejects (apply->skip): {report.false_rejects}",
        f"Label inconsistencies:       {report.label_inconsistencies}",
        f"RESULT:                      {'PASS' if report.accepted else 'FAIL'}",
    ]
    return "\n".join(lines)


def run_eval(path: Path | str = GOLDEN_SET_PATH) -> bool:
    report = evaluate(load_golden_set(path))
    print(format_report(report))
    return report.accepted


# --------------------------------------------------------------------------- #
# Skill matching (Phase 3)
# --------------------------------------------------------------------------- #


@dataclass
class SkillEvalCase:
    id: str
    profile_skills: list[str]
    jd_skill: str
    should_match: bool


@dataclass
class SkillEvalReport:
    total: int = 0
    baseline_correct: int = 0
    new_correct: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def baseline_accuracy(self) -> float:
        return self.baseline_correct / self.total if self.total else 1.0

    @property
    def new_accuracy(self) -> float:
        return self.new_correct / self.total if self.total else 1.0

    @property
    def accepted(self) -> bool:
        return self.new_accuracy >= SKILL_ACCURACY_TARGET and self.new_correct >= self.baseline_correct


def load_skill_cases(path: Path | str = SKILL_CASES_PATH) -> list[SkillEvalCase]:
    cases: list[SkillEvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(SkillEvalCase(**json.loads(line)))
    return cases


def evaluate_skill_matching(cases: list[SkillEvalCase] | None = None) -> SkillEvalReport:
    ontology = get_ontology()
    cases = cases if cases is not None else load_skill_cases()
    report = SkillEvalReport(total=len(cases))

    for case in cases:
        profile_skills = [
            Skill(name=name, canonical_name=ontology.canonicalize(name), proficiency=Proficiency.working)
            for name in case.profile_skills
        ]

        match = match_skills([case.jd_skill], [], profile_skills, ontology=ontology)
        new_match = case.jd_skill not in match.missing_must_haves

        # Old behaviour: exact lowercase name overlap only.
        base_norm = " ".join(case.jd_skill.lower().split())
        baseline_match = any(base_norm == " ".join(n.lower().split()) for n in case.profile_skills)

        report.baseline_correct += baseline_match == case.should_match
        report.new_correct += new_match == case.should_match
        if new_match != case.should_match:
            report.failures.append(case.id)

    return report


def format_skill_report(report: SkillEvalReport) -> str:
    return "\n".join(
        [
            f"Skill cases:                 {report.total}",
            f"Baseline (set-overlap):      {report.baseline_accuracy:.1%} ({report.baseline_correct}/{report.total})",
            f"Ontology matcher:            {report.new_accuracy:.1%} ({report.new_correct}/{report.total}) "
            f"target >= {SKILL_ACCURACY_TARGET:.0%}",
            f"Matcher failures:            {report.failures}",
            f"RESULT:                      {'PASS' if report.accepted else 'FAIL'}",
        ]
    )


def run_skill_eval(path: Path | str = SKILL_CASES_PATH) -> bool:
    report = evaluate_skill_matching(load_skill_cases(path))
    print(format_skill_report(report))
    return report.accepted


# --------------------------------------------------------------------------- #
# LLM precision@K (Phase 5 gate) — opt-in, needs a live model
# --------------------------------------------------------------------------- #


@dataclass
class PrecisionCaseResult:
    id: str
    label: str
    predicted_apply: bool
    score: float


@dataclass
class PrecisionReport:
    results: list[PrecisionCaseResult] = field(default_factory=list)
    k: int = 10

    @property
    def candidates(self) -> list[PrecisionCaseResult]:
        return [r for r in self.results if r.predicted_apply]

    @property
    def top_k(self) -> list[PrecisionCaseResult]:
        ranked = sorted(self.candidates, key=lambda r: r.score, reverse=True)
        return ranked[: self.k]

    @property
    def precision_at_k(self) -> float:
        top = self.top_k
        if not top:
            return 0.0
        return sum(1 for r in top if r.label == "apply") / len(top)

    @property
    def accepted(self) -> bool:
        return self.precision_at_k >= 0.8


def precision_at_k(results: list[PrecisionCaseResult], k: int = 10) -> float:
    candidates = sorted((r for r in results if r.predicted_apply), key=lambda r: r.score, reverse=True)
    top = candidates[:k]
    if not top:
        return 0.0
    return sum(1 for r in top if r.label == "apply") / len(top)


def evaluate_with_llm(cases, client, settings: Settings) -> PrecisionReport:
    """Run the full parse -> filter -> evaluate -> verify chain over the golden set."""
    from .evaluator import evaluate_job
    from .jd_parser import parse_jd
    from .ranker import final_score
    from .schemas import ExperienceLevel, Recommendation
    from .verifier import resolve_recommendation, verify_job

    profile = CandidateProfile(
        total_experience_months=int(settings.candidate.my_years_experience * 12),
        experience_level=ExperienceLevel.fresher,
    )
    cache: dict = {}
    report = PrecisionReport(k=10)
    for case in cases:
        job = Job(
            source="golden",
            title=case.title,
            company=case.company,
            location=case.location,
            url=f"golden://{case.id}",
            description_raw=case.description,
        )
        parsed = parse_jd(client, title=case.title, description=case.description)
        filter_result = apply_hard_filters(job, parsed, profile, settings)
        if not filter_result.passed:
            report.results.append(PrecisionCaseResult(case.id, case.label, False, 0.0))
            continue
        match = match_skills(parsed.must_have_skills, parsed.nice_to_have_skills, profile.skills)
        verdict = evaluate_job(
            client, profile=profile, job=job, parsed_jd=parsed, skill_match=match, settings=settings, cache=cache
        )
        verifier = verify_job(
            client, profile=profile, job=job, parsed_jd=parsed, verdict=verdict, skill_match=match, settings=settings, cache=cache
        )
        final = resolve_recommendation(verdict, verifier)
        predicted = final in {Recommendation.apply, Recommendation.strong_apply}
        report.results.append(
            PrecisionCaseResult(case.id, case.label, predicted, final_score(verdict, match, job, settings))
        )
    return report


def run_llm_eval(client=None) -> bool:
    from .config import get_settings
    from .llm import LLMClient

    settings = get_settings()
    if client is None:
        client = LLMClient(
            api_key=settings.secrets.groq_api_key,
            default_model=settings.models.extractor,
            temperature=settings.models.temperature,
        )
    report = evaluate_with_llm(load_golden_set(), client, settings)
    print(f"Golden set cases:            {len(report.results)}")
    print(f"Predicted-apply candidates:  {len(report.candidates)}")
    print(f"Precision@10:                {report.precision_at_k:.1%} (target >= 80%)")
    for item in report.top_k:
        mark = "apply" if item.label == "apply" else "skip"
        print(f"  [{item.score:5.1f}] pred_apply={item.predicted_apply} label={mark}  {item.id}")
    print(f"RESULT:                      {'PASS' if report.accepted else 'FAIL'}")
    return report.accepted
