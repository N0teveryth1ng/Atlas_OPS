"""Regression tests for `_execute_run` yield accounting (sent vs passed)."""

from __future__ import annotations

from argparse import Namespace

from atlas import cli, emailer, pipeline, sourcing
from atlas.db import connect, init_db, save_profile
from atlas.emailer import EmailError
from atlas.job_status import JobStatus
from atlas.pipeline import ProcessedJob
from atlas.schemas import (
    CandidateProfile,
    Evidence,
    EvidenceSource,
    FilterResult,
    Job,
    Recommendation,
    Verdict,
    VerifierVerdict,
)
from atlas.skills import SkillMatch
from atlas.sourcing import SourceYield, SourcingResult

JOB_ID = 10


def _shippable_result() -> ProcessedJob:
    verdict = Verdict(
        fit_score=85.0,
        seniority_fit=90,
        recommendation=Recommendation.apply,
        reasons_for=[Evidence(quote="0-2 years experience", source=EvidenceSource.jd)],
        reasons_against=[],
        seniority_assessment=Evidence(quote="Open to a fresher", source=EvidenceSource.jd),
    )
    return ProcessedJob(
        job_id=JOB_ID,
        job=Job(
            source="fake",
            url="http://x/1",
            dedupe_key="k1",
            title="Junior Python Developer",
            company="C",
            location="Remote",
        ),
        filter_result=FilterResult(passed=True),
        skill_match=SkillMatch(
            must_have_coverage=1.0, nice_to_have_coverage=0.0, missing_must_haves=[]
        ),
        verdict=verdict,
        verifier=VerifierVerdict(),
        final_recommendation=Recommendation.apply,
        score=85.0,
        status=JobStatus.ranked,
    )


def _run(tmp_path, monkeypatch, *, email: bool):
    dbfile = tmp_path / "t.db"
    setup = connect(dbfile)
    init_db(setup)
    save_profile(setup, CandidateProfile(full_name="A", target_roles=["Backend"], approved=True))
    setup.close()

    result = SourcingResult(
        source_yields=[SourceYield(source="fake", fetched=1, kept=1)],
        persisted_job_sources={JOB_ID: "fake"},
    )
    monkeypatch.setattr(cli, "connect", lambda: connect(dbfile))
    monkeypatch.setattr(cli, "_make_client", lambda: object())
    monkeypatch.setattr(sourcing, "run_sourcing", lambda *args, **kwargs: result)
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *args, **kwargs: [_shippable_result()])
    if email:

        def _boom(*args, **kwargs):
            raise EmailError("resend down")

        monkeypatch.setattr(emailer, "send_digest", _boom)

    args = Namespace(collect=True, limit=None, email=email)
    rc = cli._execute_run(args, run_kind="run")
    return rc, result


def test_no_email_leaves_sent_at_zero_but_counts_passed(tmp_path, monkeypatch):
    rc, result = _run(tmp_path, monkeypatch, email=False)
    assert rc == 0
    item = result.source_yields[0]
    assert item.passed_filters == 1
    assert item.sent == 0


def test_failed_send_leaves_sent_at_zero_but_counts_passed(tmp_path, monkeypatch):
    rc, result = _run(tmp_path, monkeypatch, email=True)
    assert rc == 0
    item = result.source_yields[0]
    assert item.passed_filters == 1
    assert item.sent == 0
