"""Tests for RecruiteeCollector — five recorded fixture cases, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from atlas.collectors import base
from atlas.collectors.base import HttpFetcher
from atlas.collectors.recruitee import (
    API_ROOT,
    RecruiteeCollector,
    parse_recruitee_date,
)
from atlas.config import Secrets, _load_secrets
from atlas.schemas import RemoteType
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def _collector(companies: list[str] | None = None, max_attempts: int = 3):
    targets = ["sysmex"] if companies is None else companies
    return RecruiteeCollector(targets, get_json=_fetcher(max_attempts))


def test_normal_case_maps_job(monkeypatch):
    log = replay.install(monkeypatch, replay.load("recruitee")["normal"])
    jobs = _collector().fetch("backend")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "recruitee"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Sysmex"
    assert job.location == "Valletta, Malta"
    assert job.source_id == "1275477"
    assert job.remote_type is RemoteType.remote
    assert job.url.endswith("/jobs/1275477-senior-backend-engineer-malta")
    assert log == [API_ROOT.format(tenant="sysmex")]


def test_offers_envelope_not_a_bare_list(monkeypatch):
    replay.install(
        monkeypatch,
        replay.Recorded(
            source="recruitee",
            case="normal",
            status=200,
            headers={},
            body=[{"id": 1, "title": "Bare list item"}],
            url="",
            provenance="synthetic",
        ),
    )
    assert _collector().fetch("") == []


def test_custom_created_at_is_parsed_as_utc(monkeypatch):
    """created_at is '2026-09-25 10:30:00 UTC' - trailing UTC marker, no ISO token."""
    replay.install(monkeypatch, replay.load("recruitee")["normal"])
    job = _collector().fetch("")[0]
    assert job.posted_at == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


def test_parse_recruitee_date_formats():
    assert parse_recruitee_date("2026-09-25 10:30:00") == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
    assert parse_recruitee_date("2026-09-25 10:30:00 UTC") == datetime(
        2026, 9, 25, 10, 30, tzinfo=UTC
    )
    assert parse_recruitee_date("25-09-2026") == datetime(2026, 9, 25, tzinfo=UTC)
    assert parse_recruitee_date("25/09/2026") == datetime(2026, 9, 25, tzinfo=UTC)
    assert parse_recruitee_date("2026-09-25T10:30:00Z") == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
    assert parse_recruitee_date("") is None
    assert parse_recruitee_date(None) is None
    assert parse_recruitee_date("nonsense") is None


def test_description_sections_are_joined(monkeypatch):
    replay.install(monkeypatch, replay.load("recruitee")["normal"])
    description = _collector().fetch("")[0].description_raw
    assert "clinical data platform" in description
    assert "distributed systems" in description
    assert "relocation support" in description


def test_api_url_is_not_used_as_the_job_url(monkeypatch):
    """The tenant's 'url' field is the API endpoint, not the application page."""
    replay.install(monkeypatch, replay.load("recruitee")["normal"])
    assert "/api/offers/" not in _collector().fetch("")[0].url


def test_remote_false_keeps_unknown(monkeypatch):
    case = replay.load("recruitee")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "offers": [{**case.body["offers"][0], "remote": False}]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("")[0].remote_type is RemoteType.unknown


def test_offers_without_careers_url_are_skipped(monkeypatch):
    case = replay.load("recruitee")["normal"]
    offer = {k: v for k, v in case.body["offers"][0].items() if k != "careers_url"}
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "offers": [offer]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("") == []


def test_company_falls_back_to_the_tenant(monkeypatch):
    case = replay.load("recruitee")["normal"]
    offer = {k: v for k, v in case.body["offers"][0].items() if k != "company_name"}
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "offers": [offer]},
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert _collector().fetch("")[0].company == "sysmex"


def test_each_tenant_is_a_separate_subdomain(monkeypatch):
    log = replay.install(monkeypatch, replay.load("recruitee")["normal"])
    _collector(companies=["sysmex", "acme"]).fetch("")
    assert log == [
        "https://sysmex.recruitee.com/api/offers",
        "https://acme.recruitee.com/api/offers",
    ]


def test_board_is_cached_across_fetches(monkeypatch):
    log = replay.install(monkeypatch, replay.load("recruitee")["normal"])
    collector = _collector()
    collector.fetch("backend")
    collector.fetch("python")
    assert len(log) == 1


def test_no_companies_makes_no_requests(monkeypatch):
    log = replay.install(monkeypatch, replay.load("recruitee")["normal"])
    assert _collector(companies=[]).fetch("") == []
    assert log == []


def test_empty_offers_returns_nothing(monkeypatch):
    replay.install(monkeypatch, replay.load("recruitee")["empty"])
    assert _collector().fetch("backend") == []


def test_malformed_case_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("recruitee")["malformed"])
    assert _collector().safe_fetch("backend") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("recruitee")["http_429"])
    assert _collector(max_attempts=3).safe_fetch("backend") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("recruitee")["http_500"])
    assert _collector(max_attempts=2).safe_fetch("backend") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("recruitee")
    log = replay.install_sequence(
        monkeypatch, [cases["http_429"], cases["normal"]], repeat_last=False
    )
    assert len(_collector().fetch("backend")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("recruitee")[case_name])
    assert _collector().safe_fetch("backend") == []


def test_query_filters_on_title(monkeypatch):
    replay.install(monkeypatch, replay.load("recruitee")["normal"])
    assert len(_collector().fetch("backend")) == 1
    assert _collector().fetch("cobol") == []


def test_collector_identity():
    collector = _collector()
    assert collector.name == "recruitee"
    assert collector.query_based is False


# --- X-Careers-Sites-Token header (401 from 2027-02-10) -------------------------

#: Stand-in value only; tests never touch a real credential.
TEST_TOKEN = "unit-test-token"


def _capture_request(monkeypatch, body: dict | None = None) -> dict[str, Any]:
    """Patch the real ``httpx.get`` so one outgoing request's shape is captured.

    Patching at the ``httpx`` layer (rather than ``base._http_get``) keeps the
    header merge under the User-Agent exercised for real.
    """
    seen: dict[str, Any] = {}

    def fake_get(url: str, params: dict | None, *, timeout: float, headers: dict) -> httpx.Response:
        seen.update(url=url, params=params, timeout=timeout, headers=dict(headers))
        payload = {"offers": []} if body is None else body
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(base.httpx, "get", fake_get)
    return seen


def test_configured_token_is_sent_as_the_careers_sites_header(monkeypatch):
    seen = _capture_request(monkeypatch)
    RecruiteeCollector(["sysmex"], careers_sites_token=TEST_TOKEN).fetch("")
    assert seen["headers"]["X-Careers-Sites-Token"] == TEST_TOKEN
    assert seen["headers"]["User-Agent"] == base.USER_AGENT
    assert seen["url"] == API_ROOT.format(tenant="sysmex")


def test_absent_token_sends_no_header_and_the_usual_request(monkeypatch):
    """The default collector (no token) is exactly today's unauthenticated GET."""
    seen = _capture_request(monkeypatch)
    RecruiteeCollector(["sysmex"]).fetch("")
    assert "X-Careers-Sites-Token" not in seen["headers"]
    assert seen["headers"] == {"User-Agent": base.USER_AGENT}
    assert seen["url"] == API_ROOT.format(tenant="sysmex")
    assert seen["params"] == {}


def test_explicitly_empty_token_sends_no_header(monkeypatch):
    seen = _capture_request(monkeypatch)
    RecruiteeCollector(["sysmex"], careers_sites_token="").fetch("")
    assert "X-Careers-Sites-Token" not in seen["headers"]
    assert seen["headers"] == {"User-Agent": base.USER_AGENT}


def test_the_token_is_a_header_and_never_part_of_the_request_line(monkeypatch):
    seen = _capture_request(monkeypatch)
    RecruiteeCollector(["sysmex"], careers_sites_token=TEST_TOKEN).fetch("")
    assert TEST_TOKEN not in seen["url"]
    assert all(TEST_TOKEN not in str(value) for value in (seen["params"] or {}).values())
    assert seen["headers"]["X-Careers-Sites-Token"] == TEST_TOKEN


def test_injected_get_json_keeps_its_own_request_shape(monkeypatch):
    """The token only shapes the default fetcher; an injected get_json is used as-is."""
    seen = _capture_request(monkeypatch)
    RecruiteeCollector(
        ["sysmex"], get_json=HttpFetcher(replay.policy()), careers_sites_token=TEST_TOKEN
    ).fetch("")
    assert "X-Careers-Sites-Token" not in seen["headers"]
    assert seen["headers"] == {"User-Agent": base.USER_AGENT}


def test_careers_sites_token_secret_defaults_to_empty():
    assert Secrets().recruitee_careers_sites_token == ""


def test_empty_careers_sites_token_is_valid(monkeypatch):
    monkeypatch.delenv("RECRUITEE_CAREERS_SITES_TOKEN", raising=False)
    assert _load_secrets().recruitee_careers_sites_token == ""


def test_careers_sites_token_loads_from_the_environment(monkeypatch):
    monkeypatch.setenv("RECRUITEE_CAREERS_SITES_TOKEN", TEST_TOKEN)
    assert _load_secrets().recruitee_careers_sites_token == TEST_TOKEN
