"""Tests for ArbeitnowCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.arbeitnow import API_URL, ArbeitnowCollector
from atlas.collectors.base import HttpFetcher
from atlas.schemas import RemoteType
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def test_normal_case_maps_job(monkeypatch):
    replay.install(monkeypatch, replay.load("arbeitnow")["normal"])
    jobs = ArbeitnowCollector(_fetcher()).fetch("data")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "arbeitnow"
    assert job.title == "Data Scientist, Product Analytics"
    assert job.company == "ElevenLabs"
    assert job.location == "Europe"
    assert job.remote_type == "remote"
    assert job.source_id == "remote-data-scientist-product-analytics-431439"
    assert job.url.endswith("/view/remote-data-scientist-product-analytics-431439")
    assert job.posted_at is not None


def test_collector_requests_the_paginated_endpoint(monkeypatch):
    log = replay.install(monkeypatch, replay.load("arbeitnow")["normal"])
    ArbeitnowCollector(_fetcher()).fetch("data")
    assert log == [API_URL]


def test_jobs_come_from_the_data_envelope(monkeypatch):
    """The payload is paginated; a bare list would raise. Prove we read `data`."""
    case = replay.load("arbeitnow")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={"unexpected": "shape"},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert ArbeitnowCollector(_fetcher()).fetch("") == []


def test_visa_sponsorship_stays_none_when_absent(monkeypatch):
    replay.install(monkeypatch, replay.load("arbeitnow")["normal"])
    collector = ArbeitnowCollector(_fetcher())
    collector.fetch("")
    assert collector.visa_sponsorship is None


def test_visa_sponsorship_is_read_when_published(monkeypatch):
    case = replay.load("arbeitnow")["normal"]
    item = dict(case.body["data"][0])
    item["visa_sponsorship"] = True
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "data": [item]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    collector = ArbeitnowCollector(_fetcher())
    collector.fetch("")
    assert collector.visa_sponsorship is True


def test_local_job_has_no_visa_field(monkeypatch):
    """Job must stay unchanged: no visa attribute leaks onto the model."""
    replay.install(monkeypatch, replay.load("arbeitnow")["normal"])
    job = ArbeitnowCollector(_fetcher()).fetch("")[0]
    assert not hasattr(job, "visa_sponsorship")
    assert "visa" not in type(job).model_fields


def test_remote_false_is_not_labelled_remote(monkeypatch):
    case = replay.load("arbeitnow")["normal"]
    item = {**case.body["data"][0], "remote": False}
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "data": [item]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert ArbeitnowCollector(_fetcher()).fetch("")[0].remote_type is RemoteType.unknown


def test_items_without_url_are_skipped(monkeypatch):
    case = replay.load("arbeitnow")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "data": [{"slug": "x", "title": "No url"}]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert ArbeitnowCollector(_fetcher()).fetch("") == []


def test_empty_case_returns_empty_list(monkeypatch):
    replay.install(monkeypatch, replay.load("arbeitnow")["empty"])
    assert ArbeitnowCollector(_fetcher()).fetch("data") == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("arbeitnow")["malformed"])
    assert ArbeitnowCollector(_fetcher()).safe_fetch("data") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("arbeitnow")["http_429"])
    assert ArbeitnowCollector(_fetcher(max_attempts=3)).safe_fetch("data") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("arbeitnow")["http_500"])
    assert ArbeitnowCollector(_fetcher(max_attempts=2)).safe_fetch("data") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("arbeitnow")
    log = replay.install_sequence(
        monkeypatch, [cases["http_429"], cases["normal"]], repeat_last=False
    )
    assert len(ArbeitnowCollector(_fetcher()).fetch("data")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("arbeitnow")[case_name])
    assert ArbeitnowCollector(_fetcher()).safe_fetch("data") == []


def test_query_filters_locally(monkeypatch):
    replay.install(monkeypatch, replay.load("arbeitnow")["normal"])
    assert len(ArbeitnowCollector(_fetcher()).fetch("elevenlabs")) == 1
    assert ArbeitnowCollector(_fetcher()).fetch("golang") == []


def test_collector_identity():
    collector = ArbeitnowCollector(_fetcher())
    assert collector.name == "arbeitnow"
    assert collector.query_based is True
