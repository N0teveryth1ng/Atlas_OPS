"""Tests for the per-source yield wiring (fetched / kept / passed / sent)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from atlas.collectors.base import Collector
from atlas.config import Settings
from atlas.schemas import Job
from atlas.sourcing import SourceYield, SourcingResult, apply_pipeline_yields, collect

NOW = datetime(2024, 6, 1, tzinfo=UTC)


class FakeCollector(Collector):
    def __init__(self, name, jobs):
        super().__init__(get_json=lambda url, params: None)
        self.name = name
        self._jobs = jobs

    def fetch(self, query):
        return [job.model_copy(deep=True) for job in self._jobs]


@dataclass
class FakeFilterResult:
    passed: bool


@dataclass
class FakeProcessed:
    job: Job
    filter_result: FakeFilterResult | None
    job_id: int | None


def _job(**kwargs) -> Job:
    base = {
        "source": "fake",
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Pune",
        "url": "http://a",
    }
    base.update(kwargs)
    return Job(**base)


def test_new_yield_fields_default_to_zero():
    item = SourceYield(source="fake")
    assert (item.fetched, item.kept, item.passed_filters, item.sent) == (0, 0, 0, 0)


def test_collect_leaves_pipeline_counts_at_zero():
    """collect() has no pipeline output, so passed/sent must stay zero."""
    source = FakeCollector("fake", [_job()])
    result = collect([source], [""], settings=Settings(), now=NOW)
    item = result.source_yields[0]
    assert item.fetched == 1
    assert item.kept == 1
    assert item.passed_filters == 0
    assert item.sent == 0


def test_apply_pipeline_yields_counts_passing_and_sent():
    result = SourcingResult(
        source_yields=[SourceYield(source="fake", fetched=3, kept=2)],
        persisted_job_sources={10: "fake", 11: "fake", 12: "fake"},
    )
    processed = [
        FakeProcessed(_job(url="http://a"), FakeFilterResult(passed=True), 10),
        FakeProcessed(_job(url="http://b"), FakeFilterResult(passed=True), 11),
        FakeProcessed(_job(url="http://c"), FakeFilterResult(passed=False), 12),
    ]
    apply_pipeline_yields(result, processed, sent_ids={10})

    item = result.source_yields[0]
    assert item.passed_filters == 2
    assert item.sent == 1


def test_shippable_ids_but_no_email_count_as_not_sent():
    result = SourcingResult(
        source_yields=[SourceYield(source="fake")], persisted_job_sources={10: "fake"}
    )
    processed = [FakeProcessed(_job(), FakeFilterResult(passed=True), 10)]
    apply_pipeline_yields(result, processed, sent_ids=set())
    assert result.source_yields[0].passed_filters == 1
    assert result.source_yields[0].sent == 0


def test_needs_review_entries_pass_filters_but_are_never_sent():
    """A review-flagged job passed the filters but is deliberately not shipped."""
    result = SourcingResult(
        source_yields=[SourceYield(source="fake")], persisted_job_sources={10: "fake"}
    )
    processed = [FakeProcessed(_job(), FakeFilterResult(passed=True), 10)]
    apply_pipeline_yields(result, processed, sent_ids={})
    assert result.source_yields[0].passed_filters == 1
    assert result.source_yields[0].sent == 0


def test_missing_filter_result_is_not_counted_as_passing():
    result = SourcingResult(
        source_yields=[SourceYield(source="fake")], persisted_job_sources={10: "fake"}
    )
    processed = [FakeProcessed(_job(), None, 10)]
    apply_pipeline_yields(result, processed, sent_ids={10})
    assert result.source_yields[0].passed_filters == 0
    assert result.source_yields[0].sent == 1


def test_rows_without_a_job_id_cannot_be_sent():
    result = SourcingResult(source_yields=[SourceYield(source="fake")])
    processed = [FakeProcessed(_job(), FakeFilterResult(passed=True), None)]
    apply_pipeline_yields(result, processed, sent_ids={10})
    assert result.source_yields[0].sent == 0


def test_jobs_from_an_unknown_source_are_ignored():
    result = SourcingResult(
        source_yields=[SourceYield(source="fake")], persisted_job_sources={10: "other"}
    )
    processed = [FakeProcessed(_job(source="other"), FakeFilterResult(passed=True), 10)]
    apply_pipeline_yields(result, processed, sent_ids={10})
    assert result.source_yields[0].passed_filters == 0
    assert result.source_yields[0].sent == 0


def test_counts_are_attributed_per_source():
    result = SourcingResult(
        source_yields=[SourceYield(source="src_a"), SourceYield(source="src_b")],
        persisted_job_sources={1: "src_a", 2: "src_a", 3: "src_b"},
    )
    processed = [
        FakeProcessed(_job(source="src_a", url="http://a"), FakeFilterResult(passed=True), 1),
        FakeProcessed(_job(source="src_a", url="http://b"), FakeFilterResult(passed=False), 2),
        FakeProcessed(_job(source="src_b", url="http://c"), FakeFilterResult(passed=True), 3),
    ]
    apply_pipeline_yields(result, processed, sent_ids={1, 3})
    a, b = result.source_yields
    assert (a.passed_filters, a.sent) == (1, 1)
    assert (b.passed_filters, b.sent) == (1, 1)


def test_apply_pipeline_yields_returns_the_same_result_object():
    result = SourcingResult(source_yields=[SourceYield(source="fake")])
    assert apply_pipeline_yields(result, [], set()) is result


def test_apply_pipeline_yields_does_not_touch_fetched_or_kept():
    """It must not recompute sourcing numbers - only add the pipeline columns."""
    result = SourcingResult(
        source_yields=[SourceYield(source="fake", fetched=7, kept=4)],
        persisted_job_sources={1: "fake"},
    )
    apply_pipeline_yields(result, [FakeProcessed(_job(), FakeFilterResult(True), 1)], {1})
    item = result.source_yields[0]
    assert item.fetched == 7
    assert item.kept == 4


def test_older_persisted_jobs_do_not_inflate_yields(tmp_path):
    from atlas.db import connect, init_db, start_run, upsert_job

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    old_job = _job(title="Old Role", company="Oldco", url="http://old")
    old_id, _ = upsert_job(conn, start_run(conn, "old-run"), old_job)

    current = FakeCollector("fake", [_job(title="New Role", company="Newco")])
    result = collect(
        [current], [""], settings=Settings(), conn=conn, run_id=start_run(conn, "run"), now=NOW
    )
    conn.close()

    assert result.persisted_job_sources
    new_id = next(iter(result.persisted_job_sources))
    processed = [
        FakeProcessed(
            _job(title="Old Role", company="Oldco", url="http://old"),
            FakeFilterResult(passed=True),
            old_id,
        ),
        FakeProcessed(
            _job(title="New Role", company="Newco"),
            FakeFilterResult(passed=True),
            new_id,
        ),
    ]
    apply_pipeline_yields(result, processed, sent_ids=set())

    item = result.source_yields[0]
    assert item.passed_filters == 1
    assert item.sent == 0
