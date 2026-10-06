"""Tests for TeamtailorCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.base import HttpFetcher
from atlas.collectors.teamtailor import FEED_URL, TeamtailorCollector
from atlas.schemas import RemoteType
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def _collector(companies: list[str] | None = None, max_attempts: int = 3):
    targets = ["varma"] if companies is None else companies
    return TeamtailorCollector(targets, get_json=_fetcher(max_attempts))


def test_normal_case_maps_job(monkeypatch):
    log = replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    jobs = _collector().fetch("fullstack")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "teamtailor"
    assert job.title == "Senior Fullstack Developer"
    assert job.company == "Varma"
    assert job.location == "Stockholm, Sweden"
    assert job.source_id == "4321987"
    assert job.remote_type is RemoteType.remote
    assert job.posted_at is not None
    assert log == [FEED_URL.format(subdomain="varma")]


def test_uses_the_public_feed_not_the_keyed_api(monkeypatch):
    """api.teamtailor.com needs a key; only the public jobs.json route is used."""
    log = replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    _collector().fetch("")
    assert all("api.teamtailor.com" not in url for url in log)


def test_jobposting_is_preferred_over_feed_level_fields(monkeypatch):
    replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    # The top-level feed title is the same here, so assert the richer field wins
    # by making _jobposting carry something the feed level does not.
    case = replay.load("teamtailor")["normal"]
    item = {**case.body["items"][0], "title": "Feed Level Title"}
    item["_jobposting"] = {**item["_jobposting"], "title": "Posting Title"}
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "items": [item]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("")[0].title == "Posting Title"


def test_falls_back_to_feed_level_fields_without_jobposting(monkeypatch):
    case = replay.load("teamtailor")["normal"]
    item = {
        "id": "3f9c1a52-7d64-4c0b-9a7e-2b8c4e1f6a90",
        "url": "https://varma.teamtailor.com/jobs/abc",
        "title": "Feed Only Title",
        "date_published": "2026-09-30T08:00:00.000Z",
    }
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "items": [item]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    job = _collector().fetch("")[0]
    assert job.title == "Feed Only Title"
    assert job.url == "https://varma.teamtailor.com/jobs/abc"
    assert job.company == "varma"
    assert job.posted_at is not None
    assert job.remote_type is RemoteType.unknown


def test_items_without_any_url_are_skipped(monkeypatch):
    case = replay.load("teamtailor")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "items": [{"id": "x", "title": "No url"}]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("") == []


def test_description_and_requirements_are_joined(monkeypatch):
    replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    description = _collector().fetch("")[0].description_raw
    assert "deploy" in description
    assert "TypeScript" in description


def test_each_subdomain_is_a_separate_request(monkeypatch):
    log = replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    _collector(companies=["varma", "storytel"]).fetch("")
    assert log == [
        "https://varma.teamtailor.com/jobs.json",
        "https://storytel.teamtailor.com/jobs.json",
    ]


def test_board_is_cached_across_fetches(monkeypatch):
    log = replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    collector = _collector()
    collector.fetch("fullstack")
    collector.fetch("python")
    assert len(log) == 1


def test_no_companies_makes_no_requests(monkeypatch):
    log = replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    assert _collector(companies=[]).fetch("") == []
    assert log == []


def test_empty_feed_returns_nothing(monkeypatch):
    replay.install(monkeypatch, replay.load("teamtailor")["empty"])
    assert _collector().fetch("fullstack") == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("teamtailor")["malformed"])
    assert _collector().safe_fetch("fullstack") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("teamtailor")["http_429"])
    assert _collector(max_attempts=3).safe_fetch("fullstack") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("teamtailor")["http_500"])
    assert _collector(max_attempts=2).safe_fetch("fullstack") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("teamtailor")
    log = replay.install_sequence(
        monkeypatch, [cases["http_429"], cases["normal"]], repeat_last=False
    )
    assert len(_collector().fetch("fullstack")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("teamtailor")[case_name])
    assert _collector().safe_fetch("fullstack") == []


def test_query_filters_on_title(monkeypatch):
    replay.install(monkeypatch, replay.load("teamtailor")["normal"])
    assert len(_collector().fetch("fullstack")) == 1
    assert _collector().fetch("cobol") == []


def test_collector_identity():
    collector = _collector()
    assert collector.name == "teamtailor"
    assert collector.query_based is False
