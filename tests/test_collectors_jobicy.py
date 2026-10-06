"""Tests for JobicyCollector — five recorded fixture cases, no network.

The ``normal``/``empty`` cases are recordings of the live API taken on 2026-10-06.
"""

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
    jobs = JobicyCollector(_fetcher()).fetch("executive")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "jobicy"
    assert job.title == "Strategic Account Executive - Central/West"
    assert job.company == "Dataiku"
    assert job.location == "USA"
    assert job.source_id == "154661"
    assert job.remote_type is RemoteType.remote
    assert job.posted_at is not None


def test_listing_url_is_kept_for_attribution(monkeypatch):
    """friendlyNotice requires linking back; the listing URL must survive mapping."""
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    job = JobicyCollector(_fetcher()).fetch("")[0]
    assert job.url == "https://jobicy.com/jobs/154661-strategic-account-executive-central-west"


def test_description_comes_from_job_description(monkeypatch):
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    job = JobicyCollector(_fetcher()).fetch("")[0]
    assert "Strategic Account Executive" in job.description_raw


def test_tag_is_sent_for_a_valid_length_query(monkeypatch):
    seen: list[dict | None] = []

    def fake(url: str, params: dict | None) -> object:
        seen.append(params)
        return {"jobs": []}

    JobicyCollector(fake).fetch("Python Developer")
    assert seen == [{"count": 50, "tag": "python developer"}]


@pytest.mark.parametrize("query", ["ai", "a" * 51])
def test_tag_is_omitted_outside_the_allowed_length(monkeypatch, query):
    """The API answers 400 for a ``tag`` shorter than 3 or longer than 50 chars."""
    seen: list[dict | None] = []

    def fake(url: str, params: dict | None) -> object:
        seen.append(params)
        return {"jobs": []}

    JobicyCollector(fake).fetch(query)
    assert seen == [{"count": 50}]


def test_no_query_sends_no_tag(monkeypatch):
    seen: list[dict | None] = []

    def fake(url: str, params: dict | None) -> object:
        seen.append(params)
        return {"jobs": []}

    JobicyCollector(fake).fetch("")
    assert seen == [{"count": 50}]


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


def test_remote_type_follows_the_endpoint_contract(monkeypatch):
    """/api/v2/remote-jobs lists remote jobs only, and the payload has no ``remote`` key."""
    case = replay.load("jobicy")["normal"]
    assert "remote" not in case.body["jobs"][0]
    replay.install(monkeypatch, case)
    jobs = JobicyCollector(_fetcher()).fetch("")
    assert len(jobs) == 2
    assert all(job.remote_type is RemoteType.remote for job in jobs)


def test_a_stray_remote_key_does_not_override_the_contract(monkeypatch):
    """A synthetic ``remote: false`` must not turn a remote job into unknown."""
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
    assert JobicyCollector(_fetcher()).fetch("")[0].remote_type is RemoteType.remote


def test_items_without_url_are_skipped(monkeypatch):
    case = replay.load("jobicy")["normal"]
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body={**case.body, "jobs": [{"id": 1, "jobTitle": "No url"}]},
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
    assert len(JobicyCollector(_fetcher()).fetch("executive")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("jobicy")[case_name])
    assert JobicyCollector(_fetcher()).safe_fetch("python") == []


def test_query_filters_locally(monkeypatch):
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    assert len(JobicyCollector(_fetcher()).fetch("dataiku")) == 2
    assert JobicyCollector(_fetcher()).fetch("cobol") == []


def test_query_matches_description_too(monkeypatch):
    """The phrase "customer success" is in neither title nor company, only the description."""
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    jobs = JobicyCollector(_fetcher()).fetch("customer success")
    assert [job.title for job in jobs] == ["Technical Account Manager - UK"]


def test_query_matches_non_mapped_fields(monkeypatch):
    """Widening regression: "Technical Support" lives only in ``jobIndustry``.

    ``tag`` narrows server-side for 3-50 character queries, but the local pass still
    runs, so it must search every textual job-content field or it would drop jobs
    the server accepted.
    """
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    jobs = JobicyCollector(_fetcher()).fetch("technical support")
    assert [job.title for job in jobs] == ["Technical Account Manager - UK"]


def test_query_matches_job_geo(monkeypatch):
    """The needle "usa" reaches job 0 through jobGeo alone, not title/company/description."""
    replay.install(monkeypatch, replay.load("jobicy")["normal"])
    assert len(JobicyCollector(_fetcher()).fetch("usa")) == 2


def test_collector_identity():
    collector = JobicyCollector(_fetcher())
    assert collector.name == "jobicy"
    assert collector.query_based is True
