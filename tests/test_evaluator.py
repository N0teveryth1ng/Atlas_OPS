from atlas.config import get_settings
from atlas.evaluator import evaluate_job
from atlas.ranker import final_score
from atlas.schemas import (
    CandidateProfile,
    Job,
    ParsedJD,
    Recommendation,
    Seniority,
    Verdict,
    VerifierVerdict,
)
from atlas.skills import SkillMatch
from atlas.verifier import resolve_recommendation, verify_job
from fake_llm import make_client

EVAL_PAYLOAD = {
    "fit_score": 82,
    "skills_fit": 80,
    "seniority_fit": 90,
    "role_fit": 85,
    "growth_fit": 70,
    "company_signal": 60,
    "recommendation": "apply",
    "reasons_for": [{"quote": "Junior Python Dev", "source": "jd"}],
    "reasons_against": [],
    "seniority_assessment": {"quote": "Junior Python Dev", "source": "jd"},
    "uncertainties": [],
}

VERIFY_PAYLOAD = {
    "veto": False,
    "downgrade_to": None,
    "reasons_against": [],
    "hidden_seniority_signals": [],
}


def _profile():
    return CandidateProfile(full_name="A", target_roles=["Backend"], email="a@b.c")


def _job():
    return Job(source="t", url="http://x", dedupe_key="k", title="Junior Python Dev", company="C")


def _parsed():
    return ParsedJD(title="Junior Python Dev", title_seniority=Seniority.junior, min_years_experience=0)


def _match():
    return SkillMatch(must_have_coverage=1.0, nice_to_have_coverage=0.0)


def test_evaluate_job_returns_verdict_and_caches():
    client = make_client(verdict=EVAL_PAYLOAD)
    cache: dict = {}
    first = evaluate_job(
        client, profile=_profile(), job=_job(), parsed_jd=_parsed(), skill_match=_match(), cache=cache
    )
    second = evaluate_job(
        client, profile=_profile(), job=_job(), parsed_jd=_parsed(), skill_match=_match(), cache=cache
    )

    assert first.recommendation == Recommendation.apply
    assert first.fit_score == 82
    assert first == second
    assert len(client._client.chat.completions.calls) == 1


def test_verify_job_returns_verifier_verdict():
    client = make_client(parsed=None, verdict=EVAL_PAYLOAD, verifier=VERIFY_PAYLOAD)
    verdict = Verdict(**EVAL_PAYLOAD)
    result = verify_job(
        client, profile=_profile(), job=_job(), parsed_jd=_parsed(), verdict=verdict, skill_match=_match()
    )
    assert isinstance(result, VerifierVerdict)
    assert result.veto is False


def test_resolve_recommendation_veto_wins():
    verdict = Verdict(fit_score=90, recommendation=Recommendation.strong_apply)
    assert resolve_recommendation(verdict, VerifierVerdict(veto=True)) == Recommendation.skip


def test_resolve_recommendation_downgrade_cannot_raise():
    verdict = Verdict(fit_score=50, recommendation=Recommendation.maybe)
    downgraded = resolve_recommendation(verdict, VerifierVerdict(downgrade_to=Recommendation.apply))
    assert downgraded == Recommendation.maybe

    lowered = resolve_recommendation(verdict, VerifierVerdict(downgrade_to=Recommendation.skip))
    assert lowered == Recommendation.skip


def test_final_score_bounds_and_weights():
    settings = get_settings()
    verdict = Verdict(fit_score=100, seniority_fit=100, recommendation=Recommendation.apply)
    match = SkillMatch(must_have_coverage=1.0, nice_to_have_coverage=1.0)
    job = Job(source="t", url="http://x", title="T", company="NotTarget")

    score = final_score(verdict, match, job, settings)
    assert 0 <= score <= 100
    # fit(.5) + coverage(.3) + seniority(.15) = 95
    assert score == 95.0
