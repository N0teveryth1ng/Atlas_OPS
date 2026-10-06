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
    jobs = _collector().fetch("machine")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "smartrecruiters"
    assert job.title == "Machine Learning Engineer (m/f/d)"
    assert job.company == "Sportradar"
    assert job.location == "Ljubljana, , Slovenia"
    assert job.source_id == "744000153670469"
    assert job.url == (
        "https://jobs.smartrecruiters.com/Sportradar/"
        "744000153670469-machine-learning-engineer-m-f-d-"
    )
    assert job.posted_at is not None
    # listing, then detail
    assert log == [
        f"{API_ROOT}/sportradar/postings",
        f"{API_ROOT}/sportradar/postings/744000153670469",
    ]


def test_title_comes_from_name_key(monkeypatch):
    """The API names the job posting `name`, not `title`."""
    replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    assert _collector().fetch("")[0].title == "Machine Learning Engineer (m/f/d)"


def test_company_comes_from_nested_object(monkeypatch):
    """`company` is an object {name, identifier}, not a companyName string."""
    case = replay.load("smartrecruiters")["normal"]
    detail = dict(case.body)
    assert detail["company"]["name"] == "Sportradar"
    replay.install_two_step(monkeypatch, case)
    assert _collector().fetch("")[0].company == "Sportradar"


def test_description_section_text_is_joined(monkeypatch):
    replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    description = _collector().fetch("")[0].description_raw
    assert "Machine Learning Engineer" in description
    assert "predictive analytics" in description
    assert "equal access" in description


def test_recorded_detail_nests_sections_under_job_ad():
    """Guards the fixture against being re-flattened away from the live shape."""
    body = replay.load("smartrecruiters")["normal"].body
    assert "sections" not in body
    assert "sections" in body["jobAd"]
    section = body["jobAd"]["sections"]["jobDescription"]
    assert isinstance(section, dict) and "text" in section


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
    assert "Machine Learning Engineer" in _collector().fetch("")[0].description_raw


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
    assert (
        _collector()
        .fetch("")[0]
        .url.endswith("/744000153670469-machine-learning-engineer-m-f-d-?oga=true")
    )


def test_posting_without_any_url_is_skipped(monkeypatch):
    """No postingUrl/applyUrl survives -> the posting is unusable and skipped."""
    case = replay.load("smartrecruiters")["normal"]
    body = {k: v for k, v in case.body.items() if k not in ("postingUrl", "applyUrl")}
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
    assert len(_collector().fetch("machine")) == 1
    # one 429, then the listing, then the detail
    assert len(log) == 3


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("smartrecruiters")[case_name])
    assert _collector().safe_fetch("backend") == []


def test_query_filters_on_title(monkeypatch):
    replay.install_two_step(monkeypatch, replay.load("smartrecruiters")["normal"])
    assert len(_collector().fetch("machine")) == 1
    assert _collector().fetch("accountant") == []


def test_collector_identity():
    collector = _collector()
    assert collector.name == "smartrecruiters"
    assert collector.query_based is False
