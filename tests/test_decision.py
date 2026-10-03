"""Tests for the Decision Engine (apply / skip / review).

Covers the pure score/veto/rule functions, the deterministic `decide` assembler,
and the LLM orchestrator (`assess_projects`, `run_critic`, `evaluate_decision`)
through a fake client. Target coverage for ``atlas/decision.py`` is >=90%.
"""

from __future__ import annotations

import pytest
from fake_llm import make_client

from atlas import decision as dec
from atlas.config import get_settings
from atlas.evidence import EvidenceValidationError
from atlas.schemas import (
    CandidateProfile,
    DecisionCritic,
    DecisionOutcome,
    Education,
    Evidence,
    EvidenceSource,
    ExperienceLevel,
    Job,
    ParsedJD,
    Proficiency,
    Project,
    ProjectRelevance,
    ProjectRelevanceItem,
    Recommendation,
    RemoteType,
    Skill,
    Verdict,
    VerifierVerdict,
    Veto,
)
from atlas.skills import SkillMatch

SETTINGS = get_settings()

# Outcome -> rank for monotonicity assertions (higher = more permissive).
_RANK = {"skip": 0, "review": 1, "apply": 2}


def _verdict(
    fit_score=80,
    seniority_fit=90,
    role_fit=80,
    growth_fit=75,
):
    return Verdict(
        fit_score=fit_score,
        skills_fit=80,
        seniority_fit=seniority_fit,
        role_fit=role_fit,
        growth_fit=growth_fit,
        company_signal=60,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="Python", source=EvidenceSource.jd)],
    )


def _verifier(veto=False, downgrade_to=None):
    v = VerifierVerdict(veto=veto, downgrade_to=downgrade_to)
    if veto:
        v.reasons_against = [Evidence(quote="Python", source=EvidenceSource.jd)]
    return v


def _profile(*, experience_months=24, skills=None, projects=None, education=None):
    return CandidateProfile(
        full_name="A",
        email="a@b.c",
        target_roles=["Backend"],
        total_experience_months=experience_months,
        experience_level=ExperienceLevel.junior,
        skills=skills
        or [Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
        projects=projects or [],
        education=education or [],
        approved=True,
    )


def _skill_match(must=1.0, nice=1.0, missing=None):
    return SkillMatch(
        must_have_coverage=must, nice_to_have_coverage=nice, missing_must_haves=missing or []
    )


def _parsed(
    *,
    min_years=None,
    red_flags=None,
    remote=RemoteType.remote,
    confidence=0.95,
    education_required=None,
    visa_relocation=None,
):
    return ParsedJD(
        title="Backend Engineer",
        title_seniority="junior",
        min_years_experience=min_years,
        max_years_experience=None,
        must_have_skills=["Python"],
        nice_to_have_skills=[],
        education_required=education_required,
        employment_type="full-time",
        location="Earth",
        remote_type=remote,
        visa_relocation=visa_relocation,
        salary=None,
        responsibilities_summary="build backend services",
        red_flags=red_flags or [],
        confidence=confidence,
        ambiguities=[],
    )


def _dims(**over):
    base = {
        "skills_core": 90,
        "seniority_fit": 90,
        "role_fit": 80,
        "project_relevance": 70,
        "skills_secondary": 60,
        "education_fit": 100,
        "growth_fit": 75,
        "logistics_fit": 100,
    }
    base.update(over)
    return base


def _project_payload(overall=80):
    return {
        "per_project": [
            {
                "project": "api service",
                "score": overall,
                "jd_evidence": [{"quote": "backend python engineer", "source": "jd"}],
                "profile_evidence": [{"quote": "api service", "source": "profile"}],
                "rationale": "direct match",
            }
        ],
        "overall": overall,
    }


class _CountingClient:
    """Fake client returning schema objects, varying project relevance per call."""

    def __init__(self, project_payloads):
        self.project_payloads = project_payloads
        self.index = 0
        self.calls: list[str] = []

    def call_json(self, *, schema, system, user, model=None, temperature=None):
        name = schema.__name__
        self.calls.append(name)
        if name == "ProjectRelevance":
            payload = self.project_payloads[self.index % len(self.project_payloads)]
            self.index += 1
            return ProjectRelevance.model_validate(payload)
        if name == "DecisionCritic":
            return DecisionCritic()
        return None


_JD = "We need a backend Python engineer with AWS."
_PROFILE = _profile(projects=[Project(title="api service", summary="Python", tech=["Python"])])


# --------------------------------------------------------------------------- #
# Dimension scoring
# --------------------------------------------------------------------------- #


def test_skills_dimension_scores():
    sm = _skill_match(must=0.9, nice=0.6)
    assert dec.skills_core_score(sm) == 90.0
    assert dec.skills_secondary_score(sm) == 60.0


def test_education_score_paths():
    profile_none = _profile()
    assert dec.education_score(_parsed(), profile_none) == 100  # no requirement

    req = _parsed()
    req.education_required = "Bachelor's degree"
    assert dec.education_score(req, _profile()) == 0  # requirement + no education
    assert (
        dec.education_score(
            req, _profile(education=[Education(degree="Bachelor of Computer Science")])
        )
        == 100
    )
    assert dec.education_score(req, _profile(education=[Education(degree="Some college")])) == 70


def test_logistics_score_paths():
    assert dec.logistics_score(_parsed()) == 100  # remote
    assert dec.logistics_score(_parsed(remote=RemoteType.onsite)) == 60
    assert dec.logistics_score(_parsed(remote=RemoteType.hybrid)) == 85
    onsite = _parsed(remote=RemoteType.onsite, visa_relocation="no sponsorship available")
    assert dec.logistics_score(onsite) == 30  # 100 - 40 - 30


def test_risk_penalty_and_cap():
    assert dec.risk_penalty(_parsed(), SETTINGS) == 0
    assert dec.risk_penalty(_parsed(red_flags=["a", "b", "c"]), SETTINGS) == 30.0
    cap = SETTINGS.decision.max_risk_penalty
    many = _parsed(red_flags=[f"f{i}" for i in range(100)])
    assert dec.risk_penalty(many, SETTINGS) == cap


def test_project_relevance_value_branches():
    assert dec.project_relevance_value(None, _profile()) == (50.0, False)
    rel = ProjectRelevance(per_project=[ProjectRelevanceItem(project="x", score=80)], overall=80)
    with_proj = dec.project_relevance_value(rel, _profile(projects=[Project(title="api")]))
    assert with_proj == (80.0, True)
    no_items = dec.project_relevance_value(
        ProjectRelevance(per_project=[], overall=0), _profile(projects=[Project(title="api")])
    )
    assert no_items == (50.0, False)


# --------------------------------------------------------------------------- #
# Vetoes
# --------------------------------------------------------------------------- #


def test_evaluate_vetoes_full_battery():
    verdict = _verdict(seniority_fit=40)  # below seniority_floor 50
    profile = _profile(experience_months=60)  # 5 years
    parsed = _parsed(min_years=3)  # needs 3y, candidate 5y -> OK
    sm = _skill_match(must=0.5)  # below coverage_floor 0.6

    vetoes = dec.evaluate_vetoes(
        parsed=parsed,
        profile=profile,
        verdict=verdict,
        verifier=_verifier(),
        skill_match=sm,
        critic=None,
        settings=SETTINGS,
    )
    rule_ids = {v.rule_id for v in vetoes}
    assert {"coverage_floor", "seniority_fit"}.issubset(rule_ids)
    assert all(v.hard for v in vetoes)

    # verifier veto WITH evidence is hard
    hard = dec.evaluate_vetoes(
        parsed=_parsed(),
        profile=_profile(experience_months=120),
        verdict=_verdict(),
        verifier=_verifier(veto=True),
        skill_match=_skill_match(),
        critic=None,
        settings=SETTINGS,
    )
    assert any(v.rule_id == "verifier_veto" and v.hard for v in hard)

    # verifier veto WITHOUT evidence is soft
    soft = dec.evaluate_vetoes(
        parsed=_parsed(),
        profile=_profile(experience_months=120),
        verdict=_verdict(),
        verifier=VerifierVerdict(veto=True),
        skill_match=_skill_match(),
        critic=None,
        settings=SETTINGS,
    )
    assert any(v.rule_id == "verifier_veto" and not v.hard for v in soft)

    # verifier downgrade_to skip is soft
    down = dec.evaluate_vetoes(
        parsed=_parsed(),
        profile=_profile(experience_months=120),
        verdict=_verdict(),
        verifier=VerifierVerdict(downgrade_to=Recommendation.skip),
        skill_match=_skill_match(),
        critic=None,
        settings=SETTINGS,
    )
    assert any(v.rule_id == "verifier_downgrade" and not v.hard for v in down)

    # critic veto with evidence is hard; without strongest_reason is soft
    critic_hard = DecisionCritic(
        propose_veto=True, strongest_reason=Evidence(quote="Python", source=EvidenceSource.jd)
    )
    ch = dec.evaluate_vetoes(
        parsed=_parsed(),
        profile=_profile(experience_months=120),
        verdict=_verdict(),
        verifier=_verifier(),
        skill_match=_skill_match(),
        critic=critic_hard,
        settings=SETTINGS,
    )
    assert any(v.rule_id == "critic_veto" and v.hard for v in ch)

    critic_soft = DecisionCritic(propose_veto=True, strongest_reason=None)
    cs = dec.evaluate_vetoes(
        parsed=_parsed(),
        profile=_profile(experience_months=120),
        verdict=_verdict(),
        verifier=_verifier(),
        skill_match=_skill_match(),
        critic=critic_soft,
        settings=SETTINGS,
    )
    assert any(v.rule_id == "critic_veto" and not v.hard for v in cs)


def test_seniority_years_allows_with_tolerance():
    v = dec.evaluate_vetoes(
        parsed=_parsed(min_years=2),
        profile=_profile(experience_months=36),
        verdict=_verdict(),
        verifier=_verifier(),
        skill_match=_skill_match(),
        critic=None,
        settings=SETTINGS,
    )
    assert not any(veto.rule_id == "seniority_years" for veto in v)


def test_seniority_years_below_tolerance_vetoes():
    v = dec.evaluate_vetoes(
        parsed=_parsed(min_years=5),
        profile=_profile(experience_months=24),
        verdict=_verdict(),
        verifier=_verifier(),
        skill_match=_skill_match(),
        critic=None,
        settings=SETTINGS,
    )
    assert any(veto.rule_id == "seniority_years" and veto.hard for veto in v)


# --------------------------------------------------------------------------- #
# Score / confidence / rule
# --------------------------------------------------------------------------- #


def test_aggregate_score_applies_penalty():
    full = {name: 100.0 for name in dec.DIMENSION_NAMES}
    score = dec.aggregate_score(full, SETTINGS.decision.weights.model_dump(), 20.0)
    assert score == 80.0  # 100 weighted - 20 penalty


def test_decision_rule_boundaries():
    settings = SETTINGS
    assert (
        dec.decision_rule(score=95, confidence=0.9, vetoes=[], settings=settings)
        == DecisionOutcome.apply
    )
    assert (
        dec.decision_rule(score=70, confidence=0.9, vetoes=[], settings=settings)
        == DecisionOutcome.review
    )
    assert (
        dec.decision_rule(score=95, confidence=0.3, vetoes=[], settings=settings)
        == DecisionOutcome.review
    )
    assert (
        dec.decision_rule(score=50, confidence=0.9, vetoes=[], settings=settings)
        == DecisionOutcome.skip
    )
    hard = [Veto(rule_id="x", reason="nope", hard=True)]
    assert (
        dec.decision_rule(score=99, confidence=0.99, vetoes=hard, settings=settings)
        == DecisionOutcome.skip
    )
    soft = [Veto(rule_id="x", reason="maybe", hard=False)]
    assert (
        dec.decision_rule(score=99, confidence=0.99, vetoes=soft, settings=settings)
        == DecisionOutcome.review
    )


def test_decide_assembles():
    decision = dec.decide(
        parsed=_parsed(),
        profile=_profile(experience_months=120),
        verdict=_verdict(seniority_fit=90),
        verifier=_verifier(),
        skill_match=_skill_match(),
        dimensions=_dims(),
        spreads={"project_relevance": 2.0},
        relevance=None,
        critic=None,
        settings=SETTINGS,
    )
    assert decision.outcome == DecisionOutcome.apply
    assert decision.match_score > 0
    assert decision.confidence > 0
    assert "project_relevance" in decision.spreads
    assert round(decision.match_score, 4) == decision.match_score


def test_decide_reasons_from_relevance():
    profile = _profile(projects=[Project(title="api", summary="Python", tech=["Python"])])
    rel = ProjectRelevance(
        per_project=[
            ProjectRelevanceItem(
                project="api",
                score=95,
                jd_evidence=[Evidence(quote="Python", source=EvidenceSource.jd)],
                profile_evidence=[Evidence(quote="Python", source=EvidenceSource.profile)],
            )
        ],
        overall=95,
    )
    decision = dec.decide(
        parsed=_parsed(),
        profile=profile,
        verdict=_verdict(seniority_fit=90),
        verifier=_verifier(),
        skill_match=_skill_match(),
        dimensions=_dims(project_relevance=95),
        spreads={},
        relevance=rel,
        critic=None,
        settings=SETTINGS,
    )
    assert any("Python" in e.quote for e in decision.reasons_for)


def test_decide_notes_when_no_projects():
    decision = dec.decide(
        parsed=_parsed(),
        profile=_profile(),
        verdict=_verdict(),
        verifier=_verifier(),
        skill_match=_skill_match(),
        dimensions=_dims(),
        settings=SETTINGS,
    )
    assert "no projects/internships" in decision.notes[0]


# --------------------------------------------------------------------------- #
# Orchestrator (FakeLLM)
# --------------------------------------------------------------------------- #


def test_evaluate_decision_apply_with_projects():
    profile = _profile(projects=[Project(title="api service", summary="Python", tech=["Python"])])
    job = Job(source="t", url="http://x", description_raw=_JD, title="Backend Engineer")
    verdict = _verdict(seniority_fit=90)
    verifier = _verifier()
    client = make_client(project_relevance=_project_payload(85))
    decision = dec.evaluate_decision(
        client,
        profile=profile,
        job=job,
        parsed=_parsed(),
        verdict=verdict,
        verifier=verifier,
        skill_match=_skill_match(),
        settings=SETTINGS,
        cache={},
    )
    assert decision.outcome == DecisionOutcome.apply
    assert "project_relevance" in decision.prompt_versions
    assert decision.prompt_versions["decision_critic"] is not None


def test_evaluate_decision_veto_skips():
    profile = _profile()
    job = Job(source="t", url="http://x", description_raw="Python", title="Backend Engineer")
    client = make_client(project_relevance=_project_payload(50))
    decision = dec.evaluate_decision(
        client,
        profile=profile,
        job=job,
        parsed=_parsed(),
        verdict=_verdict(seniority_fit=10),
        verifier=_verifier(),
        skill_match=_skill_match(),
        settings=SETTINGS,
        cache={},
    )
    assert decision.outcome == DecisionOutcome.skip
    assert any(v.rule_id == "seniority_fit" for v in decision.vetoes)


def test_evaluate_decision_coverage_veto_skips():
    profile = _profile()
    job = Job(source="t", url="http://x", description_raw="Python", title="Backend Engineer")
    client = make_client(project_relevance=_project_payload(50))
    decision = dec.evaluate_decision(
        client,
        profile=profile,
        job=job,
        parsed=_parsed(),
        verdict=_verdict(seniority_fit=90),
        verifier=_verifier(),
        skill_match=_skill_match(must=0.4),
        settings=SETTINGS,
        cache={},
    )
    assert decision.outcome == DecisionOutcome.skip
    assert any(v.rule_id == "coverage_floor" for v in decision.vetoes)


def test_assess_projects_spread_and_median():
    job = Job(source="t", url="http://x", description_raw=_JD, title="Backend Engineer")
    client = _CountingClient([_project_payload(70), _project_payload(90), _project_payload(80)])
    relevance, spread = dec.assess_projects(
        client,
        profile=_PROFILE,
        job=job,
        parsed=_parsed(),
        settings=SETTINGS,
        cache={},
    )
    assert relevance.overall == 80.0  # median
    assert spread == 20.0
    assert len(client.calls) == 3


def test_assess_projects_no_projects_short_circuits():
    profile = _profile()
    job = Job(source="t", url="http://x", description_raw="x", title="t")
    relevance, spread = dec.assess_projects(
        _CountingClient([_project_payload(90)]),
        profile=profile,
        job=job,
        parsed=_parsed(),
        settings=SETTINGS,
        cache={},
    )
    assert relevance is None
    assert spread == 0.0


def test_assess_projects_cache_hit():
    job = Job(source="t", url="http://x", description_raw=_JD, title="Backend Engineer")
    client = _CountingClient([_project_payload(80)])
    cache: dict = {}
    dec.assess_projects(
        client, profile=_PROFILE, job=job, parsed=_parsed(), settings=SETTINGS, cache=cache
    )
    calls_after_first = len(client.calls)
    dec.assess_projects(
        client, profile=_PROFILE, job=job, parsed=_parsed(), settings=SETTINGS, cache=cache
    )
    assert len(client.calls) == calls_after_first  # no new LLM call on cache hit


def test_run_critic_uses_cache():
    profile = _profile()
    job = Job(
        source="t", url="http://x", description_raw="Python backend", title="Backend Engineer"
    )
    client = _CountingClient([])
    cache: dict = {}
    first = dec.run_critic(
        client,
        profile=profile,
        job=job,
        parsed=_parsed(),
        verdict=_verdict(seniority_fit=90),
        skill_match=_skill_match(),
        settings=SETTINGS,
        cache=cache,
    )
    second = dec.run_critic(
        client,
        profile=profile,
        job=job,
        parsed=_parsed(),
        verdict=_verdict(seniority_fit=90),
        skill_match=_skill_match(),
        settings=SETTINGS,
        cache=cache,
    )
    assert first.propose_veto is False
    assert second.propose_veto is False
    assert cache  # stored


def test_call_with_evidence_retry_raises_on_bad_evidence():
    job = Job(source="t", url="http://x", description_raw="Python AWS", title="Backend Engineer")
    bad_relevance = ProjectRelevance(
        per_project=[
            ProjectRelevanceItem(
                project="api",
                score=80,
                jd_evidence=[
                    Evidence(quote="totally fabricated requirement", source=EvidenceSource.jd)
                ],
                profile_evidence=[
                    Evidence(quote="Python project stuff", source=EvidenceSource.profile)
                ],
            )
        ],
        overall=80,
    )

    class _BadClient:
        def call_json(self, **kwargs):
            return bad_relevance

    with pytest.raises(EvidenceValidationError):
        dec.assess_projects(
            _BadClient(), profile=_PROFILE, job=job, parsed=_parsed(), settings=SETTINGS, cache={}
        )


# --------------------------------------------------------------------------- #
# Property / monotonicity
# --------------------------------------------------------------------------- #


def test_monotonic_dimension_does_not_promote():
    verdict = _verdict(seniority_fit=90)
    profile = _profile(experience_months=120)
    base = dec.decide(
        parsed=_parsed(),
        profile=profile,
        verdict=verdict,
        verifier=_verifier(),
        skill_match=_skill_match(),
        dimensions=_dims(),
        settings=SETTINGS,
    )
    for name in dec.DIMENSION_NAMES:
        raised = dict(_dims())
        raised[name] = min(100.0, raised[name] + 20)
        higher = dec.decide(
            parsed=_parsed(),
            profile=profile,
            verdict=verdict,
            verifier=_verifier(),
            skill_match=_skill_match(),
            dimensions=raised,
            settings=SETTINGS,
        )
        assert _RANK[higher.outcome.value] >= _RANK[base.outcome.value]
        assert higher.match_score >= base.match_score


def test_decision_weights_must_sum_to_one():
    from pydantic import ValidationError

    from atlas.config import DecisionWeights

    with pytest.raises(ValidationError):
        DecisionWeights(skills_core=0.50)  # others unchanged -> sum > 1.0


def test_config_hash_is_stable():
    assert isinstance(dec.config_hash(), str)
    assert dec.config_hash() == dec.config_hash()
