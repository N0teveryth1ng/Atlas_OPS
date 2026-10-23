"""Tests for ``eval-labels`` - measuring the pipeline against owner labels.

Uses a tiny tmp pool + tmp labels file and a recording fake LLM so the metrics,
the read-only guarantee (SHA-256 of the real labels file before/after) and the
"label text never reaches a prompt" rule can all be asserted without a network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from atlas.config import REPO_ROOT, get_settings
from atlas.label_eval import (
    CaseResult,
    LabelEvalCase,
    Metrics,
    SystemPrediction,
    collapse_duplicates,
    format_cheap_report,
    format_report,
    load_all_records,
    load_label_cases,
    load_stored_results,
    merge_records,
    normalize_company,
    normalize_title,
    run_label_eval,
    wilson_interval,
)
from atlas.llm import GroqDailyLimitReached, LLMClient
from atlas.schemas import CandidateProfile, ExperienceLevel, Proficiency, Skill

REAL_LABELS = REPO_ROOT / "eval" / "labels.jsonl"
REAL_POOL = REPO_ROOT / "eval" / "labeling_pool.jsonl"

PARSED = {
    "title": "Backend Developer",
    "title_seniority": "junior",
    "min_years_experience": 0,
    "max_years_experience": 2,
    "must_have_skills": ["Python"],
    "nice_to_have_skills": [],
    "remote_type": "remote",
    "confidence": 0.9,
}
VERDICT = {
    "fit_score": 85,
    "skills_fit": 90,
    "seniority_fit": 95,
    "role_fit": 80,
    "growth_fit": 75,
    "company_signal": 60,
    "recommendation": "apply",
    "reasons_for": [],
    "reasons_against": [],
}
VERIFY_OK = {"veto": False, "downgrade_to": None, "reasons_against": []}


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._handler(kwargs))


class _Chat:
    def __init__(self, handler):
        self.completions = _Completions(handler)


class _FakeClient:
    def __init__(self, handler):
        self.chat = _Chat(handler)


def _canned_payload(system: str) -> dict:
    if "adversarial reviewer" in system:
        return VERIFY_OK
    if "structured requirements" in system:
        return PARSED
    if "project-to-role relevance analyst" in system:
        return {"per_project": [], "overall": 50}
    if "adversarial final decision critic" in system:
        return {"propose_veto": False, "strongest_reason": None, "reasons_against": []}
    return VERDICT


def recording_client(*, verifier=None):
    """An LLMClient fake that records every prompt and routes canned payloads."""
    state = {"prompts": []}

    def handler(kwargs):
        system = kwargs["messages"][0]["content"]
        user = kwargs["messages"][1]["content"] if len(kwargs["messages"]) > 1 else ""
        state["prompts"].append(system + "\n" + user)
        if "adversarial reviewer" in system:
            payload = verifier if verifier is not None else VERIFY_OK
        else:
            payload = _canned_payload(system)
        return json.dumps(payload)

    return LLMClient(client=_FakeClient(handler)), state


class _CallJsonClient:
    """An LLM stand-in whose ``call_json`` itself can hit the daily limit.

    The transport-level fake above raises inside ``chat.completions.create``, but
    the real ``LLMClient`` retries transport errors and only re-raises
    ``GroqDailyLimitReached`` when the error body names a per-day quota. To prove
    ``eval-labels`` stops where the provider does, this fake counts *logical*
    calls and raises from ``call_json`` - the boundary the real client crosses
    once the daily quota is spent.
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.fail_after = fail_after
        self.calls = 0
        self.tokens_used = 0
        self.prompts: list[str] = []

    def call_json(self, *, schema, system, user, model=None, temperature=None):
        self.prompts.append(system + "\n" + user)
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise GroqDailyLimitReached("Paused: Groq daily limit reached, resets in about 10.0h")
        self.calls += 1
        return schema.model_validate(_canned_payload(system))


def _profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="A",
        email="a@b.c",
        target_roles=["Backend"],
        total_experience_months=6,
        experience_level=ExperienceLevel.junior,
        approved=True,
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
    )


def _pool_entry(i: int, *, title: str | None = None, company: str | None = None) -> dict:
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    return {
        "pool_id": f"pool-{i:03d}",
        "source": "remotive",
        "source_id": str(i),
        "title": title or f"Backend Developer {i}",
        "company": company or f"Company {i}",
        "location": "Remote",
        "remote_type": "remote",
        "url": f"https://example.test/jobs/{i}",
        "urls": [f"https://example.test/jobs/{i}"],
        "posted_at": (now - timedelta(days=1)).isoformat(),
        "fetched_at": now.isoformat(),
        "description": f"Backend role {i}. Build Python services. 0-2 years experience.",
    }


def _write_pool(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "pool.jsonl"
    path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _write_labels(tmp_path: Path, entries: list[dict], labels: list[tuple[str, str]]) -> Path:
    path = tmp_path / "labels.jsonl"
    rows = []
    for entry, (label, reason) in zip(entries, labels, strict=True):
        rows.append(
            {
                "pool_id": entry["pool_id"],
                "url": entry["url"],
                "label": label,
                "reason": reason,
                "labeled_by": "human",
            }
        )
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #


def test_wilson_interval_matches_known_values() -> None:
    low, high = wilson_interval(3, 10)
    assert low == pytest.approx(0.1078, abs=1e-4)
    assert high == pytest.approx(0.6032, abs=1e-4)
    assert wilson_interval(0, 0) == (0.0, 1.0)
    # A perfect run still has a wide lower bound with a small n.
    low, high = wilson_interval(10, 10)
    assert high == pytest.approx(1.0)
    assert low < 1.0


def test_normalization_collapses_company_suffixes_and_punctuation() -> None:
    assert normalize_company("Emetlab Ltd.") == normalize_company("EMETLAB, LTD")
    assert normalize_company("Emetlab") == "emetlab"
    assert normalize_title("Backend Developer (Remote)") == "backend developer remote"


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def _result(owner: str, system: str, *, reason: str = "", company: str = "C", title: str = "T"):
    case = LabelEvalCase(
        short_id="abcd1234",
        pool_id="p",
        url="u",
        source="src",
        source_id="1",
        title=title,
        company=company,
        location="Remote",
        remote_type="remote",
        posted_at=None,
        fetched_at=None,
        description="d",
        owner_label=owner,
        owner_reason=reason,
    )
    return CaseResult(case=case, prediction=SystemPrediction(outcome=system))


def test_metrics_confusion_matrix_and_headline_numbers() -> None:
    results = [
        _result("apply", "apply"),  # true positive
        _result("apply", "review"),  # owner apply, caught as review
        _result("apply", "skip"),  # missed
        _result("skip", "apply"),  # false positive
        _result("skip", "review"),
        _result("skip", "skip"),
    ]
    m = Metrics(cases=results)
    assert m.owner_apply_total == 3
    assert m.owner_skip_total == 3
    assert m.system_apply_total == 2
    assert (m.owner_apply_system_apply, m.owner_apply_system_review, m.owner_apply_system_skip) == (
        1,
        1,
        1,
    )
    assert (m.owner_skip_system_apply, m.owner_skip_system_review, m.owner_skip_system_skip) == (
        1,
        1,
        1,
    )
    # precision APPLY = 1 owner-apply of 2 system APPLYs
    assert m.precision_apply_numerator == 1 and m.system_apply_total == 2
    assert m.precision_apply == pytest.approx(0.5)
    # recall owner-apply = (TP + review) / owner apply = 2/3
    assert m.recall_apply_numerator == 2 and m.owner_apply_total == 3
    assert m.recall_apply == pytest.approx(2 / 3)


def test_metrics_seniority_false_pass_counts_review_and_apply_only() -> None:
    results = [
        _result("skip", "apply", reason="senior level, needs 5 years"),
        _result("skip", "review", reason="too much experience required"),
        _result("skip", "skip", reason="senior role"),
        _result("skip", "review", reason="wrong role, helpdesk"),
        _result("apply", "apply", reason="senior title but fine"),
    ]
    false_pass = Metrics(cases=results).seniority_false_pass
    shorts = {id(r) for r in false_pass}
    assert len(false_pass) == 2
    assert results[0] in false_pass
    assert results[1] in false_pass
    assert results[2] not in false_pass  # skip/skip is not a false pass
    assert results[3] not in false_pass  # no seniority/experience keyword
    _ = shorts


def test_per_source_and_per_rule_breakdowns() -> None:
    r1 = _result("skip", "skip")
    r1.prediction.fired_rules = ["senior_title", "coverage_floor"]
    r2 = _result("apply", "apply")
    r2.case.source = "other"
    r2.prediction.fired_rules = ["coverage_floor"]
    m = Metrics(cases=[r1, r2])
    assert set(m.per_source()) == {"src", "other"}
    assert m.per_rule() == {"coverage_floor": 2, "senior_title": 1}


def test_collapse_duplicates_keeps_first_per_company_title() -> None:
    a = _result("skip", "skip", company="Emetlab Ltd.", title="Backend Dev")
    b = _result("apply", "apply", company="Emetlab", title="Backend Dev")
    c = _result("apply", "apply", company="Other Co", title="Backend Dev")
    deduped = collapse_duplicates([a, b, c])
    assert deduped == [a, c]


# --------------------------------------------------------------------------- #
# End-to-end replay (fake LLM, tmp DB)
# --------------------------------------------------------------------------- #


def test_run_label_eval_replays_pipeline_and_reports(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(3)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(
        tmp_path, entries, [("apply", "good fit"), ("skip", "senior"), ("apply", "ok")]
    )
    client, _state = recording_client()

    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.cases) == 3
    assert report.remaining == 0
    assert report.paused_reason is None
    assert report.llm_calls > 0
    # Every job survived the cheap gate, so each went through the LLM stages.
    assert all(c.prediction.reached_llm for c in report.cases)
    text = format_report(report)
    assert "Confusion matrix" in text
    assert "precision of APPLY" in text
    assert "recall of owner-apply jobs (APPLY+REVIEW)" in text
    assert "seniority false-pass" in text
    assert "95% Wilson interval" in text
    assert "Per source" in text
    assert "Per rule" in text
    assert "Disagreements" in text
    assert "Excluding duplicate postings" in text


def test_cheap_rejection_costs_zero_llm_calls(tmp_path: Path) -> None:
    entries = [_pool_entry(0, title="Senior Backend Engineer")]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("skip", "senior")])
    client, state = recording_client()

    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.cases) == 1
    case = report.cases[0]
    assert case.prediction.outcome == "skip"
    assert case.prediction.llm_calls == 0
    assert case.prediction.fired_rule == "senior_title"
    assert state["prompts"] == []


def test_stop_at_daily_limit_reports_remaining_and_resumes(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(3)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("apply", "b"), ("apply", "c")])
    # A full job costs four logical calls (parse, evaluate, verify, decide). Let
    # the first job finish, then the provider reports the daily quota is spent on
    # the next job's first call.
    client = _CallJsonClient(fail_after=4)

    paused = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert paused.paused_reason is not None
    assert paused.paused_reason.startswith("daily limit")
    assert paused.remaining == 2
    assert [c.case.pool_id for c in paused.cases] == [entries[0]["pool_id"]]

    # A later run with a healthy client picks up the jobs the pause left behind.
    resumed = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=_CallJsonClient(),
        settings=get_settings(),
        profile=_profile(),
    )
    assert resumed.paused_reason is None
    assert resumed.remaining == 0
    assert [c.case.pool_id for c in resumed.cases] == [entry["pool_id"] for entry in entries]
    assert resumed.cases[0].prediction.outcome == paused.cases[0].prediction.outcome


def test_reads_the_approved_profile_from_the_real_db(tmp_path: Path, monkeypatch) -> None:
    from atlas.db import connect, init_db, save_profile

    real = tmp_path / "real.db"
    conn = connect(real)
    init_db(conn)
    save_profile(conn, _profile())
    conn.close()
    monkeypatch.setenv("ATLAS_DB", str(real))

    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=_CallJsonClient(),
        settings=get_settings(),
        database=tmp_path / "replay.db",
    )
    # Without this the profile was read from the (empty) private replay DB.
    assert report.profile_source == "real atlas.db (latest approved profile)"
    assert report.paused_reason is None
    assert len(report.cases) == 1


def test_no_approved_profile_returns_instead_of_leaking_the_temp_db(
    tmp_path: Path, monkeypatch
) -> None:
    from atlas.db import connect, init_db

    real = tmp_path / "empty.db"
    conn = connect(real)
    init_db(conn)
    conn.close()
    monkeypatch.setenv("ATLAS_DB", str(real))

    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    # database=None forces the private temp dir: the old early return left the
    # connection open and TemporaryDirectory.cleanup() raised PermissionError on
    # Windows, masking the report.
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=_CallJsonClient(),
        settings=get_settings(),
    )
    assert report.paused_reason == "no approved profile available"
    assert report.cases == []


# --------------------------------------------------------------------------- #
# Per-job result store (resumable) and the cheap-only run
# --------------------------------------------------------------------------- #


def test_results_file_records_every_job_with_its_audit_details(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit"), ("skip", "senior")])
    results = tmp_path / "results.jsonl"
    client, _state = recording_client()

    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert report.resumed_from_store == 0
    records = load_all_records(results)
    assert len(records) == 2
    by_pool = {r["pool_id"]: r for r in records}
    for entry in entries:
        record = by_pool[entry["pool_id"]]
        assert record["short_id"]
        assert record["title"] == entry["title"]
        assert record["company"] == entry["company"]
        assert record["owner_label"] in {"apply", "skip"}
        assert record["owner_reason"]
        assert record["system_decision"] in {"apply", "review", "skip"}
        assert "score" in record and "confidence" in record
        assert isinstance(record["fired_rules"], list)
        assert isinstance(record["vetoes"], list)
        assert isinstance(record["must_have_skills"], list)
        assert isinstance(record["nice_to_have_skills"], list)
        assert isinstance(record["skill_details"], list)
        assert record["must_have_coverage_denominator"] is not None
        assert isinstance(record["dimension_scores"], dict)
        assert record["llm_calls"] >= 0
        assert record["tokens"] >= 0
    # The parsed must-have list and the per-skill credit are kept.
    record = next(r for r in records if r["pool_id"] == entries[0]["pool_id"])
    assert record["must_have_skills"] == ["Python"]
    assert record["skill_details"][0]["skill"] == "Python"
    assert record["skill_details"][0]["credit"] > 0
    assert record["skill_details"][0]["why"]
    assert record["must_have_coverage_numerator"] == pytest.approx(1.0)
    assert record["must_have_coverage_denominator"] == 1


def test_second_run_resumes_from_the_store_and_spends_no_tokens(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(3)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("apply", "b"), ("skip", "c")])
    results = tmp_path / "results.jsonl"

    first = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert first.resumed_from_store == 0
    assert first.llm_calls > 0

    client, state = recording_client()
    second = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert second.resumed_from_store == 3
    assert second.llm_calls == 0
    assert second.tokens_used == 0
    assert state["prompts"] == []
    assert [c.prediction.outcome for c in second.cases] == [
        c.prediction.outcome for c in first.cases
    ]
    # The store keeps exactly one record per job.
    assert len(load_all_records(results)) == 3


def test_a_paused_run_resumes_without_repeating_measured_jobs(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(3)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("apply", "b"), ("apply", "c")])
    results = tmp_path / "results.jsonl"

    # The first job finishes, then the provider reports the daily quota is spent.
    paused = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=_CallJsonClient(fail_after=4),
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert paused.paused_reason is not None
    assert paused.remaining == 2
    assert len(load_all_records(results)) == 1

    resumed = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=_CallJsonClient(),
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert resumed.resumed_from_store == 1
    assert resumed.remaining == 0
    assert len(load_all_records(results)) == 3
    assert len(resumed.cases) == 3


def test_no_resume_re_measures_every_job(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("skip", "b")])
    results = tmp_path / "results.jsonl"

    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    again = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
        resume=False,
    )
    assert again.resumed_from_store == 0
    assert again.llm_calls > 0


def test_cheap_only_runs_no_llm_stage_and_reports_the_split(tmp_path: Path) -> None:
    entries = [
        _pool_entry(0),
        _pool_entry(1),
        _pool_entry(2, title="Senior Backend Engineer"),
    ]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(
        tmp_path, entries, [("apply", "fit"), ("skip", "not for me"), ("apply", "senior but fit")]
    )
    results = tmp_path / "results.jsonl"
    client, state = recording_client()

    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        cheap_only=True,
        results_path=results,
    )
    assert state["prompts"] == []
    assert report.llm_calls == 0
    assert report.tokens_used == 0
    assert len(report.cases) == 3
    assert len(report.cheap_rejected) == 1
    assert len(report.cheap_passed) == 2
    rejected = report.cheap_rejected[0]
    assert rejected.case.owner_label == "apply"
    assert "senior_title" in rejected.prediction.fired_rules
    # A job that passed the cheap gate was never decided: it must not read as SKIP.
    assert {c.prediction.outcome for c in report.cheap_passed} == {"unknown"}
    assert rejected.prediction.outcome == "skip"

    text = format_cheap_report(report)
    assert "rejected by cheap filters" in text
    assert "passed (needs LLM)" in text
    assert "senior_title" in text
    # The owner-apply job the cheap gate rejects is called out by id.
    assert rejected.case.short_id in text


def test_cheap_only_writes_the_results_file_without_measuring(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("skip", "b")])
    results = tmp_path / "results.jsonl"

    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        cheap_only=True,
        results_path=results,
    )
    records = load_all_records(results)
    assert len(records) == 2
    assert all(r["mode"] == "cheap" for r in records)
    # A cheap record must not make the job look measured to a full run.
    assert load_stored_results(results, mode="full") == {}


def test_a_full_run_keeps_the_cheap_records_in_the_same_file(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("skip", "b")])
    results = tmp_path / "results.jsonl"

    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        cheap_only=True,
        results_path=results,
    )
    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    records = load_all_records(results)
    assert {r["mode"] for r in records} == {"cheap", "full"}
    assert len(records) == 4
    assert len(load_stored_results(results, mode="full")) == 2


def test_merge_records_replaces_only_the_records_of_the_same_mode() -> None:
    existing = [
        {"short_id": "a", "mode": "full", "score": 1},
        {"short_id": "b", "mode": "full", "score": 2},
        {"short_id": "a", "mode": "cheap", "score": 3},
    ]
    merged = merge_records(existing, [{"short_id": "a", "mode": "full", "score": 9}], mode="full")
    by_key = {(r["short_id"], r["mode"]): r for r in merged}
    assert by_key[("a", "full")]["score"] == 9
    assert by_key[("b", "full")]["score"] == 2
    assert by_key[("a", "cheap")]["score"] == 3
    assert len(merged) == 3


def test_cheap_only_never_writes_the_labels_file(tmp_path: Path) -> None:
    before = _sha(REAL_LABELS)
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("skip", "b")])

    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=recording_client()[0],
        settings=get_settings(),
        profile=_profile(),
        cheap_only=True,
        results_path=tmp_path / "results.jsonl",
    )
    assert _sha(REAL_LABELS) == before


# --------------------------------------------------------------------------- #
# Read-only guarantee and prompt hygiene
# --------------------------------------------------------------------------- #


def test_real_labels_sha256_unchanged_and_no_writes(tmp_path: Path) -> None:
    assert REAL_LABELS.exists()
    before = _sha(REAL_LABELS)
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit"), ("skip", "senior")])
    client, _state = recording_client()

    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        database=tmp_path / "eval.db",
    )
    assert _sha(REAL_LABELS) == before


def test_owner_label_text_never_reaches_a_prompt(tmp_path: Path) -> None:
    secret_reason = "ZZTOP-SECRET-REASON-DO-NOT-SHOW"
    entries = [_pool_entry(i) for i in range(2)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("skip", secret_reason), ("apply", secret_reason)])
    client, state = recording_client()

    run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    joined = "\n".join(state["prompts"])
    assert secret_reason not in joined
    assert "labeled_by" not in joined


def test_load_label_cases_joins_pool_and_labels(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(4)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries[:2], [("apply", "x"), ("skip", "y")])
    cases = load_label_cases(labels, pool)
    assert [c.pool_id for c in cases] == [entries[0]["pool_id"], entries[1]["pool_id"]]
    assert cases[0].owner_label == "apply"
    assert cases[1].source == "remotive"


def test_real_files_untouched_by_load(tmp_path: Path) -> None:
    """Loading the real files does not modify them."""
    if not (REAL_LABELS.exists() and REAL_POOL.exists()):
        pytest.skip("real eval files absent")
    before_l = _sha(REAL_LABELS)
    before_p = _sha(REAL_POOL)
    load_label_cases(REAL_LABELS, REAL_POOL)
    assert _sha(REAL_LABELS) == before_l
    assert _sha(REAL_POOL) == before_p


def test_missing_labels_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_label_cases(Path("does-not-exist.jsonl"), Path("also-missing.jsonl"))


# --------------------------------------------------------------------------- #
# Fake transports for status / per-call testing
# --------------------------------------------------------------------------- #


class _HTTP429(Exception):
    """An exception carrying an HTTP 429 response (not a daily-limit pause)."""

    def __init__(self) -> None:
        self.response = type("resp", (), {"status_code": 429, "headers": {}})()


def _client_with_handler(handler):
    """An LLMClient whose transport routes every call through ``handler``."""
    return LLMClient(client=_FakeClient(handler))


def _handler_invalid_json(kwargs):
    return "{not valid json"


def _handler_429(kwargs):
    raise _HTTP429()


# --------------------------------------------------------------------------- #
# Status: evaluated vs incomplete
# --------------------------------------------------------------------------- #


def test_status_evaluated_when_valid_llm_output(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "good fit")])
    client, _ = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.cases) == 1
    pred = report.cases[0].prediction
    assert pred.status == "evaluated"
    assert pred.reason is None
    assert pred.prompt_version == "2"
    assert pred.as_of is not None
    assert len(pred.per_call) > 0


def test_status_incomplete_when_invalid_json(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    client = _client_with_handler(_handler_invalid_json)
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.cases) == 1
    pred = report.cases[0].prediction
    assert pred.status == "incomplete"
    assert pred.reason is not None and "LLM error" in pred.reason
    assert pred.outcome == "skip"
    assert pred.llm_calls >= 1
    assert any(not c["ok"] for c in pred.per_call)


def test_status_incomplete_when_429_error(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    client = _client_with_handler(_handler_429)
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.cases) == 1
    pred = report.cases[0].prediction
    assert pred.status == "incomplete"
    assert pred.reason is not None and "LLM error" in pred.reason
    assert any(429 in (c.get("http_errors") or []) for c in pred.per_call)


def test_per_call_details_recorded(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    client, _ = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    pred = report.cases[0].prediction
    assert len(pred.per_call) > 0
    for call in pred.per_call:
        assert "stage" in call
        assert "model" in call
        assert isinstance(call.get("tokens", 0), int)
        assert isinstance(call.get("transport_retries", 0), int)
        assert isinstance(call.get("validation_attempts", 0), int)
        assert isinstance(call.get("validation_failures", 0), int)
        assert isinstance(call.get("ok"), bool)


# --------------------------------------------------------------------------- #
# Prompt version and resume
# --------------------------------------------------------------------------- #


def test_prompt_version_stored(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    client, _ = recording_client()
    results = tmp_path / "results.jsonl"
    _report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    record = load_all_records(results)[0]
    assert record["prompt_version"] == "2"
    assert record["status"] == "evaluated"
    assert record["as_of"] is not None
    assert isinstance(record["per_call"], list)


def test_older_version_record_is_rerun(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps(
            {
                "short_id": entries[0]["pool_id"][:8],
                "pool_id": entries[0]["pool_id"],
                "title": entries[0]["title"],
                "company": entries[0]["company"],
                "url": entries[0]["url"],
                "source": entries[0]["source"],
                "owner_label": "apply",
                "owner_reason": "fit",
                "mode": "full",
                "system_decision": "apply",
                "score": 85.0,
                "confidence": 0.9,
                "fired_rules": [],
                "vetoes": [],
                "reasons": [],
                "must_have_skills": ["Python"],
                "nice_to_have_skills": [],
                "skill_details": [],
                "missing_skills": [],
                "must_have_coverage": 1.0,
                "must_have_coverage_numerator": 1.0,
                "must_have_coverage_denominator": 1,
                "nice_to_have_coverage": None,
                "dimension_scores": {},
                "llm_calls": 4,
                "tokens": 1000,
                "reached_llm": True,
                "cheap_status": "passed",
                "regex_min_years": None,
                "regex_max_years": None,
                "regex_title_seniority": "junior",
                "regex_remote_type": "remote",
                "regex_years_quote": None,
                "status": "evaluated",
                "reason": None,
                "prompt_version": "1",
                "as_of": "2026-01-01T00:00:00+00:00",
                "per_call": [],
            }
        ),
        encoding="utf-8",
    )
    client, state = recording_client()
    _report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert state["prompts"] != []
    assert _report.resumed_from_store == 0


def test_current_version_record_is_skipped(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps(
            {
                "short_id": entries[0]["pool_id"][:8],
                "pool_id": entries[0]["pool_id"],
                "title": entries[0]["title"],
                "company": entries[0]["company"],
                "url": entries[0]["url"],
                "source": entries[0]["source"],
                "owner_label": "apply",
                "owner_reason": "fit",
                "mode": "full",
                "system_decision": "apply",
                "score": 85.0,
                "confidence": 0.9,
                "fired_rules": [],
                "vetoes": [],
                "reasons": [],
                "must_have_skills": ["Python"],
                "nice_to_have_skills": [],
                "skill_details": [],
                "missing_skills": [],
                "must_have_coverage": 1.0,
                "must_have_coverage_numerator": 1.0,
                "must_have_coverage_denominator": 1,
                "nice_to_have_coverage": None,
                "dimension_scores": {},
                "llm_calls": 4,
                "tokens": 1000,
                "reached_llm": True,
                "cheap_status": "passed",
                "regex_min_years": None,
                "regex_max_years": None,
                "regex_title_seniority": "junior",
                "regex_remote_type": "remote",
                "regex_years_quote": None,
                "status": "evaluated",
                "reason": None,
                "prompt_version": "2",
                "as_of": "2026-01-01T00:00:00+00:00",
                "per_call": [],
            }
        ),
        encoding="utf-8",
    )
    client, state = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert state["prompts"] == []
    assert report.resumed_from_store == 1


# --------------------------------------------------------------------------- #
# Ordering, dedup, flags
# --------------------------------------------------------------------------- #


def test_duplicates_collapsed_by_default(tmp_path: Path) -> None:
    entries = [
        _pool_entry(0, company="Emetlab Ltd.", title="Backend Dev"),
        _pool_entry(1, company="Emetlab", title="Backend Dev"),
        _pool_entry(2, company="Other Co", title="Backend Dev"),
    ]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("skip", "x"), ("apply", "y"), ("apply", "z")])
    client, _state = recording_client()
    results = tmp_path / "results.jsonl"
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
    )
    assert len(report.cases) == 2
    titles = {c.case.title for c in report.cases}
    assert titles == {"Backend Dev"}
    companies = {c.case.company for c in report.cases}
    assert "Emetlab Ltd." in companies or "Emetlab" in companies
    assert "Other Co" in companies


def test_include_duplicates_keeps_all(tmp_path: Path) -> None:
    entries = [
        _pool_entry(0, company="Emetlab Ltd.", title="Backend Dev"),
        _pool_entry(1, company="Emetlab", title="Backend Dev"),
    ]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("skip", "x"), ("apply", "y")])
    client, _ = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        include_duplicates=True,
    )
    assert len(report.cases) == 2


def test_apply_first_order(tmp_path: Path) -> None:
    entries = [
        _pool_entry(0, title="Skip Job A"),
        _pool_entry(1, title="Apply Job B"),
        _pool_entry(2, title="Skip Job C"),
        _pool_entry(3, title="Apply Job D"),
    ]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(
        tmp_path,
        entries,
        [("skip", "a"), ("apply", "b"), ("skip", "c"), ("apply", "d")],
    )
    client, _ = recording_client()
    results = tmp_path / "results.jsonl"
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        results_path=results,
        limit=4,
    )
    ordered_labels = [c.case.owner_label for c in report.cases]
    apply_indices = [i for i, l in enumerate(ordered_labels) if l == "apply"]
    skip_indices = [i for i, l in enumerate(ordered_labels) if l == "skip"]
    assert all(i < max(skip_indices) for i in apply_indices) or not skip_indices


def test_only_apply_flag(tmp_path: Path) -> None:
    entries = [
        _pool_entry(0, title="Apply Job"),
        _pool_entry(1, title="Skip Job"),
    ]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a"), ("skip", "s")])
    client, _ = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        only_apply=True,
    )
    assert len(report.cases) == 1
    assert report.cases[0].case.owner_label == "apply"


def test_limit_flag(tmp_path: Path) -> None:
    entries = [_pool_entry(i) for i in range(5)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "a")] * 5)
    client, _ = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
        limit=2,
    )
    assert len(report.cases) == 2


# --------------------------------------------------------------------------- #
# Metrics exclude incomplete jobs
# --------------------------------------------------------------------------- #


def test_metrics_exclude_incomplete_jobs(tmp_path: Path) -> None:
    entries = [
        _pool_entry(0, title="Valid Job"),
        _pool_entry(1, title="Broken Job"),
    ]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit"), ("apply", "fit")])
    call_count = {"n": 0}

    def handler(kwargs):
        call_count["n"] += 1
        user = kwargs["messages"][1]["content"] if len(kwargs["messages"]) > 1 else ""
        if "Broken Job" in user:
            return "{not valid json"
        return json.dumps(_canned_payload(kwargs["messages"][0]["content"]))

    client = _client_with_handler(handler)
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.cases) == 2
    evaluated = [c for c in report.cases if c.prediction.status == "evaluated"]
    incomplete = [c for c in report.cases if c.prediction.status == "incomplete"]
    assert len(evaluated) == 1
    assert len(incomplete) == 1
    m = report.metrics_evaluated
    assert m.system_apply_total >= 0
    assert len(report.incomplete_cases) == 1
    assert report.incomplete_cases[0].prediction.reason is not None


def test_incomplete_jobs_listed_with_reasons(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    client = _client_with_handler(_handler_invalid_json)
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    assert len(report.incomplete_cases) == 1
    assert report.incomplete_cases[0].prediction.reason is not None
    text = format_report(report)
    assert "Incomplete jobs" in text


def test_cost_report_per_stage(tmp_path: Path) -> None:
    entries = [_pool_entry(0)]
    pool = _write_pool(tmp_path, entries)
    labels = _write_labels(tmp_path, entries, [("apply", "fit")])
    client, _ = recording_client()
    report = run_label_eval(
        labels_path=labels,
        pool_path=pool,
        client=client,
        settings=get_settings(),
        profile=_profile(),
    )
    text = format_report(report)
    assert "Cost report" in text
    assert "calls" in text and "tokens" in text
