"""Tests for JobicyCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.base import HttpFetcher
from atlas.collectors.jobicy import API_URL, JobicyCollector
from atlas.schemas import RemoteType
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def test_normal_case_maps_job(monkeypatch):
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    jobs = JobicyCollector(_fetcher()).fetch("customer")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "jobicy"
    assert job.title == "Customer Success Engineer"
    assert job.company == "Algolia"
    assert job.location == "Europe"
    assert job.source_id == "154612"
    assert job.remote_type is RemoteType.remote
    assert job.posted_at is not None


def test_listing_url_is_kept_for_attribution(monkeypatch):
    """friendlyNotice requires linking back; the listing URL must survive mapping."""
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    job = JobicyCollector(_fetcher()).fetch("")[0]
    assert job.url == "https://jobicy.com/jobs/154612-customer-success-engineer"


def test_query_and_page_size_are_sent(monkeypatch):
    seen: list[dict | None] = []

    def fake(url: str, params: dict | None) -> object:
        seen.append(params)
        return {"jobs": []}

    JobicyCollector(fake).fetch("Python Developer")
    assert seen == [{"count": 50, "q": "python developer"}]


def test_endpoint_is_the_v2_remote_jobs_route(monkeypatch):
    log = replay.install(monkeypatch, replay.load("jobicy")["normal"])
    JobicyCollector(_fetcher()).fetch("")
    assert log == [API_URL]


def test_numeric_id_is_stringified(monkeypatch):
    job = JobicyCollector(_fetcher())
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    assert isinstance(job.fetch("")[0].source_id, str)


def test_missing_id_leaves_source_id_none(monkeypatch):
    case = replay.load("jobicy")["normal"]
    item = {k: v for k, v in case.body["jobs"][0].items() if k != "id"}
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "jobs": [item]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert JobicyCollector(_fetcher()).fetch("")[0].source_id is None


def test_remote_false_keeps_unknown(monkeypatch):
    case = replay.load("jobicy")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "jobs": [{**case.body["jobs"][0], "remote": False}]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert JobicyCollector(_fetcher()).fetch("")[0].remote_type is RemoteType.unknown


def test_items_without_url_are_skipped(monkeypatch):
    case = replay.load("jobicy")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "jobs": [{"id": 1, "title": "No url"}]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert JobicyCollector(_fetcher()).fetch("") == []


def test_missing_jobs_key_is_not_an_error(monkeypatch):
    replay.install(monkeypatch, replay.load("jobicy")["empty"])
    assert JobicyCollector(_fetcher()).fetch("python") == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("jobicy")["malformed"])
    assert JobicyCollector(_fetcher()).safe_fetch("python") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("jobicy")["http_429"])
    assert JobicyCollector(_fetcher(max_attempts=3)).safe_fetch("python") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("jobicy")["http_500"])
    assert JobicyCollector(_fetcher(max_attempts=2)).safe_fetch("python") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("jobicy")
    log = replay.install_sequence(
        monkeypatch, [cases["http_429"], cases["normal"]], repeat_last=False
    )
    assert len(JobicyCollector(_fetcher()).fetch("customer")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("jobicy")[case_name])
    assert JobicyCollector(_fetcher()).safe_fetch("python") == []


def test_query_filters_locally(monkeypatch):
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    assert len(JobicyCollector(_fetcher()).fetch("algolia")) == 1
    assert JobicyCollector(_fetcher()).fetch("cobol") == []


def test_collector_identity():
    collector = JobicyCollector(_fetcher())
    assert collector.name == "jobicy"
    assert collector.query_based is True
