"""Tests for WorkableCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.base import HttpFetcher
from atlas.collectors.workable import API_ROOT, WorkableCollector
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def _collector(companies: list[str] | None = None, max_attempts: int = 3):
    targets = ["huckberry"] if companies is None else companies
    return WorkableCollector(targets, get_json=_fetcher(max_attempts))


def test_normal_case_maps_job(monkeypatch):
    log = replay.install(monkeypatch, replay.load("workable")["normal"])
    jobs = _collector().fetch("designer")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "workable"
    assert job.title == "Associate Designer, Apparel"
    assert job.company == "huckberry"
    assert job.location == "Austin, Texas, United States"
    assert job.source_id == "62180FD5F9"
    assert job.posted_at is not None
    assert log == [f"{API_ROOT}/huckberry"]


def test_v1_widget_endpoint_with_details(monkeypatch):
    """v3 returns 404; the collector must use the v1 widget route with details."""
    seen: list[tuple[str, dict | None]] = []

    def fake(url: str, params: dict | None) -> object:
        seen.append((url, params))
        return {"jobs": []}

    WorkableCollector(["huckberry"], get_json=fake).fetch("")
    assert seen == [(f"{API_ROOT}/huckberry", {"details": "true"})]


def test_description_is_the_only_text_field(monkeypatch):
    """Live payloads have one HTML description; there is no requirements/benefits."""
    replay.install(monkeypatch, replay.load("workable")["normal"])
    description = _collector().fetch("")[0].description_raw
    assert "apparel meant to be lived in" in description
    assert "menswear" in description


def test_location_is_built_from_city_state_country(monkeypatch):
    """No location key exists; city/state/country are composed into the field."""
    case = replay.load("workable")["normal"]
    item = {k: v for k, v in case.body["jobs"][0].items() if k != "locations"}
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
    assert _collector().fetch("")[0].location == "Austin, Texas, United States"


def test_location_is_none_when_no_place_available(monkeypatch):
    case = replay.load("workable")["normal"]
    item = {
        k: v
        for k, v in case.body["jobs"][0].items()
        if k not in ("locations", "city", "state", "country")
    }
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
    assert _collector().fetch("")[0].location is None


def test_application_url_is_the_fallback(monkeypatch):
    case = replay.load("workable")["normal"]
    item = {k: v for k, v in case.body["jobs"][0].items() if k != "url"}
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
    assert _collector().fetch("")[0].url.endswith("/apply")


def test_items_without_any_url_are_skipped(monkeypatch):
    case = replay.load("workable")["normal"]
    item = {k: v for k, v in case.body["jobs"][0].items() if k not in ("url", "application_url")}
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
    assert _collector().fetch("") == []


def test_missing_shortcode_leaves_source_id_none(monkeypatch):
    case = replay.load("workable")["normal"]
    item = {k: v for k, v in case.body["jobs"][0].items() if k != "shortcode"}
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
    assert _collector().fetch("")[0].source_id is None


def test_board_is_cached_across_fetches(monkeypatch):
    log = replay.install(monkeypatch, replay.load("workable")["normal"])
    collector = _collector()
    collector.fetch("designer")
    collector.fetch("designer")
    assert len(log) == 1


def test_no_companies_makes_no_requests(monkeypatch):
    log = replay.install(monkeypatch, replay.load("workable")["normal"])
    assert _collector(companies=[]).fetch("") == []
    assert log == []


def test_each_company_is_a_separate_request(monkeypatch):
    log = replay.install(monkeypatch, replay.load("workable")["normal"])
    _collector(companies=["huckberry", "other"]).fetch("")
    assert log == [f"{API_ROOT}/huckberry", f"{API_ROOT}/other"]


def test_empty_case_returns_empty_list(monkeypatch):
    replay.install(monkeypatch, replay.load("workable")["empty"])
    assert _collector().fetch("designer") == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("workable")["malformed"])
    assert _collector().safe_fetch("designer") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("workable")["http_429"])
    assert _collector(max_attempts=3).safe_fetch("designer") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("workable")["http_500"])
    assert _collector(max_attempts=2).safe_fetch("designer") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("workable")
    log = replay.install_sequence(
        monkeypatch, [cases["http_429"], cases["normal"]], repeat_last=False
    )
    assert len(_collector().fetch("designer")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("workable")[case_name])
    assert _collector().safe_fetch("designer") == []


def test_query_filters_on_title(monkeypatch):
    replay.install(monkeypatch, replay.load("workable")["normal"])
    assert len(_collector().fetch("designer")) == 1
    assert _collector().fetch("cobol") == []


def test_collector_identity():
    collector = _collector()
    assert collector.name == "workable"
    assert collector.query_based is False
