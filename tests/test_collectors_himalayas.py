"""Tests for HimalayasCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.base import HttpFetcher, RetryableStatusError
from atlas.collectors.himalayas import HimalayasCollector
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def test_normal_case_maps_job(monkeypatch):
    replay.install(monkeypatch, replay.load("himalayas")["normal"])
    jobs = HimalayasCollector(_fetcher()).fetch("python")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "himalayas"
    assert job.title == "Python Developer"
    assert job.company == "Kodland"
    assert job.location == "UK"
    assert job.source_id == "1031423"
    assert job.url.endswith("/apply")
    assert job.posted_at is not None


def test_normal_case_hits_the_documented_endpoint_once(monkeypatch):
    log = replay.install(monkeypatch, replay.load("himalayas")["normal"])
    HimalayasCollector(_fetcher()).fetch("python")
    assert log == ["https://himalayas.app/jobs/api/search"]


def test_query_matches_description_too(monkeypatch):
    replay.install(monkeypatch, replay.load("himalayas")["normal"])
    # "Django" appears only in the skills array of the recorded job, so it must
    # not match: the collector filters on mapped fields, not unmapped extras.
    assert HimalayasCollector(_fetcher()).fetch("django") == []
    assert len(HimalayasCollector(_fetcher()).fetch("learning experiences")) == 1


def test_query_filters_out_non_matching(monkeypatch):
    replay.install(monkeypatch, replay.load("himalayas")["normal"])
    assert HimalayasCollector(_fetcher()).fetch("rust") == []


def test_query_is_sent_to_the_api(monkeypatch):
    seen: list[dict | None] = []

    def fake(url: str, params: dict | None) -> object:
        seen.append(params)
        return {"jobs": [], "total": 0}

    HimalayasCollector(fake).fetch("Python Developer")
    assert seen == [{"limit": 20, "query": "python developer"}]


def test_empty_query_returns_everything(monkeypatch):
    replay.install(monkeypatch, replay.load("himalayas")["normal"])
    assert len(HimalayasCollector(_fetcher()).fetch("")) == 1


def test_empty_case_returns_empty_list(monkeypatch):
    replay.install(monkeypatch, replay.load("himalayas")["empty"])
    assert HimalayasCollector(_fetcher()).fetch("python") == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("himalayas")["malformed"])
    assert HimalayasCollector(_fetcher()).safe_fetch("python") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("himalayas")["http_429"])
    assert HimalayasCollector(_fetcher(max_attempts=3)).safe_fetch("python") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("himalayas")["http_500"])
    assert HimalayasCollector(_fetcher(max_attempts=2)).safe_fetch("python") == []
    assert len(log) == 2


def test_items_without_a_url_are_skipped(monkeypatch):
    case = replay.load("himalayas")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={"jobs": [{"id": "1", "title": "No link"}], "total": 1},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert HimalayasCollector(_fetcher()).fetch("") == []


def test_guid_is_used_when_application_link_is_missing(monkeypatch):
    case = replay.load("himalayas")["normal"]
    item = dict(case.body["jobs"][0])
    item.pop("applicationLink")
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={"jobs": [item], "total": 1},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    jobs = HimalayasCollector(_fetcher()).fetch("")
    assert jobs[0].url == item["guid"]


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("himalayas")
    log = replay.install_sequence(
        monkeypatch,
        [cases["http_429"], cases["normal"]],
        repeat_last=False,
    )
    assert len(HimalayasCollector(_fetcher()).fetch("python")) == 1
    assert len(log) == 2


def test_retryable_status_error_carries_retry_after():
    error = RetryableStatusError(429, retry_after=60.0)
    assert error.status_code == 429
    assert error.retry_after == 60.0
    assert "Retry-After" in str(error)


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("himalayas")[case_name])
    assert HimalayasCollector(_fetcher()).safe_fetch("python") == []


def test_collector_identity():
    collector = HimalayasCollector(_fetcher())
    assert collector.name == "himalayas"
    assert collector.query_based is True
