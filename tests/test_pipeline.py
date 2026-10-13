import json
import logging

import pytest
from fake_llm import make_client

from atlas.config import get_settings, settings_for_profile
from atlas.db import (
    connect,
    get_job_status,
    get_latest_filter_checks,
    init_db,
    log_stage,
    save_profile,
    set_job_status,
    start_run,
    upsert_job,
)
from atlas.job_status import IllegalTransition, JobStatus, reopen_for_refilter, reset_for_retry
from atlas.pipeline import (
    RESUMABLE_STATUSES,
    LLMCallBudgetExceeded,
    _BudgetedClient,
    load_jobs,
    process_job,
    reapply_filters,
    run_pipeline,
)
from atlas.schemas import (
    CandidateProfile,
    Job,
    ParsedJD,
    Proficiency,
    Recommendation,
    Skill,
)

PARSED = {
    "title": "Junior Python Developer",
    "title_seniority": "junior",
    "min_years_experience": 0,
    "max_years_experience": 2,
    "must_have_skills": ["Python"],
    "nice_to_have_skills": [],
    "remote_type": "remote",
    "confidence": 0.9,
}

EVAL = {
    "fit_score": 85,
    "skills_fit": 90,
    "seniority_fit": 95,
    "role_fit": 80,
    "growth_fit": 75,
    "company_signal": 60,
    "recommendation": "apply",
    "reasons_for": [{"quote": "0-2 years experience", "source": "jd"}],
    "reasons_against": [],
}

VERIFY_OK = {"veto": False, "downgrade_to": None, "reasons_against": []}
VERIFY_VETO = {
    "veto": True,
    "downgrade_to": "skip",
    "reasons_against": [{"quote": "0-2 years experience", "source": "jd"}],
}


def _setup(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    profile = CandidateProfile(
        full_name="A",
        email="a@b.c",
        target_roles=["Backend"],
        total_experience_months=6,
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
    )
    save_profile(conn, profile)
    run_id = start_run(conn, "run")
    job = Job(
        source="t",
        url="http://x",
        dedupe_key="k",
        title="Junior Python Developer",
        company="C",
        location="Remote",
        remote_type="remote",
        description_raw="We are hiring a Junior Python Developer. 0-2 years experience. Fully remote.",
    )
    job_id, _ = upsert_job(conn, run_id, job)
    return conn, run_id, profile, job_id, job


def test_reset_for_retry_rewinds_only_pre_decision_states():
    for status in (
        JobStatus.parsed,
        JobStatus.passed_filters,
        JobStatus.evaluated,
        JobStatus.verified,
    ):
        assert reset_for_retry(status) == JobStatus.new
    assert reset_for_retry(JobStatus.new) == JobStatus.new
    for terminal in (
        JobStatus.rejected,
        JobStatus.needs_review,
        JobStatus.ranked,
        JobStatus.sent,
    ):
        with pytest.raises(IllegalTransition):
            reset_for_retry(terminal)


def test_process_job_happy_path(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)

    result = process_job(client, job_id, job, profile, get_settings(), conn=conn)

    assert result.filter_result.passed
    assert result.verdict is not None
    assert result.final_recommendation == Recommendation.apply
    assert result.score > 0
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM verifications").fetchone()[0] == 1


def test_process_job_verifier_veto_skips(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)
    client = make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_VETO)

    result = process_job(client, job_id, job, profile, get_settings(), conn=conn)

    assert result.final_recommendation == Recommendation.skip


def test_process_job_filter_rejection_short_circuits(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)
    job.title = "Senior Python Developer"
    senior_parsed = {**PARSED, "title_seniority": "senior"}
    client = make_client(parsed=senior_parsed, verdict=EVAL, verifier=VERIFY_OK)

    result = process_job(client, job_id, job, profile, get_settings(), conn=conn)

    assert result.filter_result.passed is False
    assert result.verdict is None
    assert result.final_recommendation == Recommendation.skip
    rejection_rules = json.dumps([r.rule_id for r in result.filter_result.rejections])
    assert "senior_title" in rejection_rules


def _client():
    return make_client(parsed=PARSED, verdict=EVAL, verifier=VERIFY_OK)


def _mark_rejected(conn, job_id: int) -> None:
    """Drive the state machine to a terminal state the legal way."""
    set_job_status(conn, job_id, JobStatus.parsed)
    set_job_status(conn, job_id, JobStatus.rejected)


def test_load_jobs_returns_only_unprocessed_jobs_by_default(tmp_path):
    conn, run_id, _profile, job_id, job = _setup(tmp_path)
    second_id, _ = upsert_job(
        conn, run_id, job.model_copy(update={"url": "http://x/2", "dedupe_key": "k2"})
    )
    _mark_rejected(conn, job_id)

    assert [jid for jid, _ in load_jobs(conn)] == [second_id]
    # The default is a filter, not a deletion: every row stays reachable.
    assert len(load_jobs(conn, statuses=None)) == 2
    conn.close()


def test_run_pipeline_skips_jobs_that_cannot_restart(tmp_path, caplog):
    """A stored rejected job must not be re-fed to the state machine."""
    conn, run_id, profile, job_id, _job = _setup(tmp_path)
    _mark_rejected(conn, job_id)

    results = run_pipeline(conn, run_id, profile, get_settings(), _client())

    assert results == []
    assert "failed" not in caplog.text
    assert "illegal job transition" not in caplog.text
    assert (
        conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()["status"]
        == "rejected"
    )
    conn.close()


def test_run_limit_counts_unprocessed_jobs_only(tmp_path):
    """`run --limit N` must mean N jobs it can actually start, not N newest rows."""
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    fresh: list[int] = []
    for i in range(4):
        jid, _ = upsert_job(
            conn, run_id, job.model_copy(update={"url": f"http://x/{i}", "dedupe_key": f"k{i}"})
        )
        fresh.append(jid)
    for jid in fresh[:2]:
        _mark_rejected(conn, jid)

    results = run_pipeline(conn, run_id, profile, get_settings(), _client(), limit=2)

    processed_ids = {result.job_id for result in results}
    assert len(processed_ids) == 2
    assert processed_ids.isdisjoint(set(fresh[:2]))
    conn.close()


def test_skip_log_counts_only_non_new_rows(tmp_path, caplog):
    """Jobs excluded by `--limit` are not "already processed"."""
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    stored: list[int] = []
    for i in range(4):
        jid, _ = upsert_job(
            conn, run_id, job.model_copy(update={"url": f"http://x/{i}", "dedupe_key": f"k{i}"})
        )
        stored.append(jid)
    for jid in stored[:2]:
        _mark_rejected(conn, jid)

    # The assertion below inspects INFO logs, so force the level on for the
    # duration of the call: otherwise this test only passes when an earlier
    # test happened to lower the global logging level (order-dependent).
    with caplog.at_level(logging.INFO, logger="atlas.pipeline"):
        run_pipeline(conn, run_id, profile, get_settings(), _client(), limit=2)

    assert "skipping 2 stored job(s)" in caplog.text
    conn.close()


def test_load_jobs_undecided_keeps_stuck_jobs_drops_decided(tmp_path):
    """Resume must pick up jobs stranded mid-pipeline but never re-rule a decided one."""
    conn, run_id, _profile, fresh_id, job = _setup(tmp_path)
    stuck_id, _ = upsert_job(
        conn, run_id, job.model_copy(update={"url": "http://x/2", "dedupe_key": "k2"})
    )
    set_job_status(conn, stuck_id, JobStatus.parsed)
    rejected_id, _ = upsert_job(
        conn, run_id, job.model_copy(update={"url": "http://x/3", "dedupe_key": "k3"})
    )
    _mark_rejected(conn, rejected_id)
    decided_id, _ = upsert_job(
        conn, run_id, job.model_copy(update={"url": "http://x/4", "dedupe_key": "k4"})
    )
    set_job_status(conn, decided_id, JobStatus.parsed)
    set_job_status(conn, decided_id, JobStatus.passed_filters)
    log_stage(conn, "decisions", job_id=decided_id, data={"outcome": "skip", "match_score": 0.0})

    got = {jid for jid, _ in load_jobs(conn, statuses=RESUMABLE_STATUSES, undecided=True)}

    assert got == {fresh_id, stuck_id}
    conn.close()


def test_run_pipeline_resume_reaches_a_decision_for_stuck_job(tmp_path):
    """A job left at `parsed` by an interrupted run is finished on the next run."""
    conn, run_id, profile, _fresh, job = _setup(tmp_path)
    stuck_id, _ = upsert_job(
        conn, run_id, job.model_copy(update={"url": "http://x/2", "dedupe_key": "k2"})
    )
    set_job_status(conn, stuck_id, JobStatus.parsed)

    results = run_pipeline(conn, run_id, profile, get_settings(), _client(), resume=True)

    assert stuck_id in {result.job_id for result in results}
    assert (
        conn.execute("SELECT COUNT(*) FROM decisions WHERE job_id = ?", (stuck_id,)).fetchone()[0]
        == 1
    )
    conn.close()


def test_run_pipeline_progress_reports_stage_counts(tmp_path):
    conn, run_id, profile, _fresh, _job = _setup(tmp_path)
    events: list[dict] = []

    run_pipeline(
        conn,
        run_id,
        profile,
        get_settings(),
        _client(),
        on_progress=events.append,
    )

    assert events, "progress callback must fire at least once"
    assert events[0]["total"] >= 1
    final = events[-1]
    assert final["done"] == final["total"]
    assert final["decided"] >= 1
    conn.close()


def test_process_job_stores_filter_checks_on_pass(tmp_path):
    conn, _run, profile, job_id, job = _setup(tmp_path)

    process_job(_client(), job_id, job, profile, get_settings(), conn=conn)

    by_rule = {check["rule_id"]: check for check in get_latest_filter_checks(conn, job_id)}
    assert by_rule["too_much_experience"]["checked"] is True
    assert by_rule["too_much_experience"]["value"] == "min_years 0"
    # Rules that could not run are recorded with a reason, not silently dropped.
    assert by_rule["employment_type_mismatch"]["checked"] is False
    assert "not stated" in by_rule["employment_type_mismatch"]["skipped_reason"]
    assert by_rule["salary_below_floor"]["checked"] is False
    assert by_rule["deal_breaker_skill"]["checked"] is False
    assert by_rule["stale_job"]["checked"] is False
    conn.close()


def test_cheap_rejection_is_tagged_and_keeps_the_same_evidence_format(tmp_path):
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    senior_id, _ = upsert_job(
        conn,
        run_id,
        job.model_copy(
            update={"url": "http://x/2", "dedupe_key": "k2", "title": "Senior Python Developer"}
        ),
    )

    run_pipeline(conn, run_id, profile, get_settings(), _client())

    row = conn.execute(
        "SELECT rule_id, evidence, data_json FROM filter_results WHERE job_id = ? AND passed = 0",
        (senior_id,),
    ).fetchone()
    assert row["rule_id"] == "senior_title"
    assert row["evidence"] == "title seniority=senior"
    assert json.loads(row["data_json"])["cheap"] is True
    conn.close()


def test_post_llm_rejection_is_not_tagged_cheap(tmp_path):
    conn, run_id, profile, job_id, _job = _setup(tmp_path)
    settings = get_settings().model_copy(deep=True)
    settings.filters.deal_breaker_skills = ["python"]

    run_pipeline(conn, run_id, profile, settings, _client())

    row = conn.execute(
        "SELECT rule_id, data_json FROM filter_results WHERE job_id = ? AND passed = 0",
        (job_id,),
    ).fetchone()
    assert row["rule_id"] == "deal_breaker_skill"
    assert json.loads(row["data_json"])["cheap"] is False
    conn.close()


def test_cheap_rejected_job_is_not_parsed(tmp_path):
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    senior_id, _ = upsert_job(
        conn,
        run_id,
        job.model_copy(
            update={"url": "http://x/2", "dedupe_key": "k2", "title": "Senior Python Developer"}
        ),
    )

    run_pipeline(conn, run_id, profile, get_settings(), _client())

    assert (
        conn.execute("SELECT COUNT(*) FROM parsed_jds WHERE job_id = ?", (senior_id,)).fetchone()[0]
        == 0
    )
    assert (
        conn.execute("SELECT status FROM jobs WHERE id = ?", (senior_id,)).fetchone()["status"]
        == "rejected"
    )
    conn.close()


def test_max_jobs_per_run_counts_only_survivors(tmp_path):
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    for i in range(3):
        upsert_job(
            conn,
            run_id,
            job.model_copy(update={"url": f"http://x/{i}", "dedupe_key": f"k{i}"}),
        )
    settings = get_settings().model_copy(deep=True)
    settings.run.max_jobs_per_run = 2

    results = run_pipeline(conn, run_id, profile, settings, _client())

    assert len(results) == 2
    conn.close()


def test_limit_overrides_max_jobs_per_run(tmp_path):
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    for i in range(3):
        upsert_job(
            conn,
            run_id,
            job.model_copy(update={"url": f"http://x/{i}", "dedupe_key": f"k{i}"}),
        )
    settings = get_settings().model_copy(deep=True)
    settings.run.max_jobs_per_run = 1

    results = run_pipeline(conn, run_id, profile, settings, _client(), limit=2)

    assert len(results) == 2
    conn.close()


def test_budgeted_client_refuses_to_exceed_the_cap():
    wrapped = _BudgetedClient(_client(), 2)
    wrapped.call_json(schema=ParsedJD, system="structured requirements", user="x")
    wrapped.call_json(schema=ParsedJD, system="structured requirements", user="x")
    with pytest.raises(LLMCallBudgetExceeded):
        wrapped.call_json(schema=ParsedJD, system="structured requirements", user="x")


def test_run_pipeline_stops_when_llm_budget_is_exhausted(tmp_path, caplog):
    conn, run_id, profile, _job_id, job = _setup(tmp_path)
    for i in range(2):
        upsert_job(
            conn,
            run_id,
            job.model_copy(update={"url": f"http://x/{i}", "dedupe_key": f"k{i}"}),
        )
    settings = get_settings().model_copy(deep=True)
    settings.run.max_llm_calls_per_run = 1

    with caplog.at_level(logging.WARNING, logger="atlas.pipeline"):
        results = run_pipeline(conn, run_id, profile, settings, _client())

    assert "budget" in caplog.text.lower()
    assert results == []
    conn.close()


def _force_status(conn, job_id, status):
    """Set a job's status directly, to simulate a state a prior run left behind."""
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status.value, job_id))
    conn.commit()


def test_reopen_for_refilter_revives_only_rejected_and_needs_review():
    assert reopen_for_refilter(JobStatus.rejected) == JobStatus.new
    assert reopen_for_refilter(JobStatus.needs_review) == JobStatus.new
    for state in (
        JobStatus.new,
        JobStatus.parsed,
        JobStatus.passed_filters,
        JobStatus.evaluated,
        JobStatus.verified,
        JobStatus.ranked,
        JobStatus.sent,
    ):
        with pytest.raises(IllegalTransition):
            reopen_for_refilter(state)


def test_reapply_filters_reopens_a_job_only_when_the_new_profile_passes_it(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    run_id = start_run(conn, "run")
    job = Job(
        source="t",
        url="http://x/berlin",
        dedupe_key="berlin",
        title="Backend Engineer",
        company="C",
        location="Berlin",
        remote_type="onsite",
        description_raw="Backend Engineer role in our Berlin office.",
    )
    job_id, _ = upsert_job(conn, run_id, job)
    _force_status(conn, job_id, JobStatus.rejected)

    pune = CandidateProfile(
        full_name="A", email="a@b.c", target_roles=["Backend"], locations=["Pune"]
    )
    berlin = pune.model_copy(update={"locations": ["Berlin"]})

    # The job still fails the location filter under the old profile.
    unchanged = reapply_filters(conn, pune, settings_for_profile(pune), reason="profile approved")
    assert unchanged.examined == 1
    assert unchanged.reopened == 0
    assert get_job_status(conn, job_id) == JobStatus.rejected

    # The new profile matches, so the same job is re-opened and the reason stored.
    revived = reapply_filters(conn, berlin, settings_for_profile(berlin), reason="profile approved")
    assert revived.reopened == 1
    assert revived.reopened_ids == [job_id]
    assert get_job_status(conn, job_id) == JobStatus.new
    event = conn.execute(
        "SELECT event, reason, data_json FROM job_events WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert event["event"] == "reopened_for_refilter"
    assert event["reason"] == "profile approved"
    assert json.loads(event["data_json"]) == {"from": "rejected", "to": "new"}
    conn.close()


def test_reapply_filters_reopens_a_needs_review_job_that_now_passes(tmp_path):
    conn, _run, profile, job_id, _job = _setup(tmp_path)
    _force_status(conn, job_id, JobStatus.needs_review)

    summary = reapply_filters(conn, profile, settings_for_profile(profile), reason="run started")

    assert summary.reopened == 1
    assert get_job_status(conn, job_id) == JobStatus.new
    conn.close()


def test_reapply_filters_never_reopens_a_shipped_job(tmp_path):
    conn, _run, profile, job_id, _job = _setup(tmp_path)
    # A ranked/sent job is already decided and shipped; even forced to a state
    # the undecided loader would return, it must be left exactly as it is.
    _force_status(conn, job_id, JobStatus.sent)

    summary = reapply_filters(conn, profile, settings_for_profile(profile), reason="run started")

    assert summary.reopened == 0
    assert summary.examined == 0
    assert get_job_status(conn, job_id) == JobStatus.sent
    conn.close()
