"""Tests for SmartRecruitersCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.base import HttpFetcher
from atlas.collectors.smartrecruiters import API_ROOT, SmartRecruitersCollector
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def _collector(companies: list[str] | None = None, max_attempts: int = 3):
    targets = ["sportradar"] if companies is None else companies
    return SmartRecruitersCollector(targets, get_json=_fetcher(max_attempts))


def test_normal_case_maps_job(monkeypatch):
    log = replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    jobs = _collector().fetch("backend")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "smartrecruiters"
    assert job.title == "Software Engineer, Backend"
    assert job.company == "Sportradar"
    assert job.location == "Valletta, Malta"
    assert job.source_id == "744000153369739"
    assert job.url == "https://jobs.sportradar.com/744000153369739-software-engineer-backend"
    assert job.posted_at is not None
    # listing, then detail
    assert log == [
        f"{API_ROOT}/sportradar/postings",
        f"{API_ROOT}/sportradar/postings/744000153369739",
    ]


def test_description_sections_are_joined(monkeypatch):
    replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    description = _collector().fetch("")[0].description_raw
    assert "live betting feeds" in description
    assert "Remote-friendly within Europe" in description
    assert "simple designs" in description
    assert "on-call" in description


def test_recorded_detail_nests_sections_under_job_ad():
    """Guards the fixture against being re-flattened away from the live shape."""
    body = replay.load("smartrecruiters")["normal"].body
    assert "sections" not in body
    assert "sections" in body["jobAd"]


def test_flattened_sections_are_still_accepted(monkeypatch):
    """A tenant serving `sections` at the top level must still map cleanly."""
    case = replay.load("smartrecruiters")["normal"]
    body = dict(case.body)
    body["sections"] = body.pop("jobAd")["sections"]
    replay.install_two_step(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body=body,
            listing=case.listing,
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert "live betting feeds" in _collector().fetch("")[0].description_raw


def test_apply_url_is_the_fallback_when_no_posting_url(monkeypatch):
    case = replay.load("smartrecruiters")["normal"]
    body = {k: v for k, v in case.body.items() if k != "postingUrl"}
    replay.install_two_step(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body=body,
            listing=case.listing,
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("")[0].url.endswith("/apply/744000153369739")


def test_listing_only_url_is_used_if_detail_is_empty(monkeypatch):
    case = replay.load("smartrecruiters")["normal"]
    replay.install_two_step(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={},
            listing=case.listing,
            url=case.url,
            provenance=case.provenance,
        ),
    )
    job = _collector().fetch("")[0]
    assert job.url == case.listing["content"][0]["postingUrl"]
    assert job.title == "Software Engineer, Backend"


def test_listings_without_an_id_are_skipped(monkeypatch):
    case = replay.load("smartrecruiters")["normal"]
    replay.install_two_step(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body=case.body,
            listing={"content": [{"title": "No id"}], "total": 1},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("") == []


def test_board_is_cached_across_fetches(monkeypatch):
    log = replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    collector = _collector()
    collector.fetch("backend")
    collector.fetch("python")
    assert len(log) == 2


def test_empty_listing_returns_nothing(monkeypatch):
    case = replay.load("smartrecruiters")["empty"]
    replay.install_two_step(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body=case.body,
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("") == []


def test_no_companies_makes_no_requests(monkeypatch):
    log = replay.install(monkeypatch, replay.load("smartrecruiters")["normal"])
    assert _collector(companies=[]).fetch("") == []
    assert log == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("smartrecruiters")["malformed"])
    assert _collector().safe_fetch("backend") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("smartrecruiters")["http_429"])
    assert _collector(max_attempts=3).safe_fetch("backend") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("smartrecruiters")["http_500"])
    assert _collector(max_attempts=2).safe_fetch("backend") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("smartrecruiters")
    log = replay.install_two_step(monkeypatch, cases["normal"], prefix=[cases["http_429"]])
    assert len(_collector().fetch("backend")) == 1
    # one 429, then the listing, then the detail
    assert len(log) == 3


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("smartrecruiters")[case_name])
    assert _collector().safe_fetch("backend") == []


def test_query_filters_on_title(monkeypatch):
    replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    assert len(_collector().fetch("backend")) == 1
    assert _collector().fetch("cobol") == []


def test_collector_identity():
    collector = _collector()
    assert collector.name == "smartrecruiters"
    assert collector.query_based is False
