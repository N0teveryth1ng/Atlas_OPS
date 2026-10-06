"""Tests for WeWorkRemotelyCollector — five recorded fixture cases, no network."""

from __future__ import annotations

import pytest

from atlas.collectors.base import HttpFetcher
from atlas.collectors.weworkremotely import FEED_URL, WeWorkRemotelyCollector, _split_title
from atlas.schemas import RemoteType
from tests import replay


def _fetcher(max_attempts: int = 3) -> HttpFetcher:
    return HttpFetcher(replay.policy(max_attempts=max_attempts))


def test_normal_case_maps_job(monkeypatch):
    replay.install(monkeypatch, replay.load("weworkremotely")["normal"])
    jobs = WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("python")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "weworkremotely"
    assert job.company == "EPAM Systems"
    assert job.title == "Senior Python Engineer"
    assert job.location == "Anywhere"
    assert job.source_id == "wWR-2026-0412"
    assert job.remote_type is RemoteType.remote
    assert job.url.endswith("epam-systems-senior-python-engineer")
    assert job.posted_at is not None
    assert job.posted_at.year == 2026


def test_feed_url_is_the_documented_rss_route(monkeypatch):
    log = replay.install(monkeypatch, replay.load("weworkremotely")["normal"])
    WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("")
    assert log == [FEED_URL]


def test_title_is_split_into_company_and_role():
    assert _split_title("EPAM Systems: Senior Python Engineer") == (
        "EPAM Systems",
        "Senior Python Engineer",
    )
    assert _split_title("Bare Title") == (None, "Bare Title")
    assert _split_title("") == (None, None)


def test_description_is_preserved(monkeypatch):
    replay.install(monkeypatch, replay.load("weworkremotely")["normal"])
    job = WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("")[0]
    assert "distributed team" in job.description_raw


def test_country_falls_back_to_region(monkeypatch):
    case = replay.load("weworkremotely")["normal"]
    body = case.body.replace("<country>Anywhere</country>\n", "")
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body=body,
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("")[0].location == "Europe"


def test_items_without_link_are_skipped(monkeypatch):
    case = replay.load("weworkremotely")["normal"]
    body = case.body.replace(
        "      <link>https://weworkremotely.com/remote-jobs/epam-systems-senior-python-engineer</link>\n",
        "",
    )
    replay.install(
        monkeypatch,
        replay.Recorded(
            source=case.source,
            case=case.case,
            status=case.status,
            headers=case.headers,
            body=body,
            url=case.url,
            provenance=case.provenance,
        ),
    )
    assert WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("") == []


def test_empty_channel_yields_nothing(monkeypatch):
    replay.install(monkeypatch, replay.load("weworkremotely")["empty"])
    assert WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("python") == []


def test_malformed_xml_degrades_to_empty(monkeypatch):
    replay.install(monkeypatch, replay.load("weworkremotely")["malformed"])
    assert WeWorkRemotelyCollector(_fetcher().fetch_text).safe_fetch("python") == []


def test_429_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("weworkremotely")["http_429"])
    collector = WeWorkRemotelyCollector(_fetcher(max_attempts=3).fetch_text)
    assert collector.safe_fetch("python") == []
    assert len(log) == 3


def test_500_is_retried_then_yields_empty(monkeypatch):
    log = replay.install(monkeypatch, replay.load("weworkremotely")["http_500"])
    collector = WeWorkRemotelyCollector(_fetcher(max_attempts=2).fetch_text)
    assert collector.safe_fetch("python") == []
    assert len(log) == 2


def test_429_recovers_when_the_limit_lifts(monkeypatch):
    cases = replay.load("weworkremotely")
    log = replay.install_sequence(
        monkeypatch, [cases["http_429"], cases["normal"]], repeat_last=False
    )
    assert len(WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("python")) == 1
    assert len(log) == 2


@pytest.mark.parametrize("case_name", ["malformed", "http_429", "http_500"])
def test_failure_cases_never_raise(monkeypatch, case_name):
    replay.install(monkeypatch, replay.load("weworkremotely")[case_name])
    assert WeWorkRemotelyCollector(_fetcher().fetch_text).safe_fetch("python") == []


def test_query_filters_locally(monkeypatch):
    replay.install(monkeypatch, replay.load("weworkremotely")["normal"])
    assert len(WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("epam")) == 1
    assert WeWorkRemotelyCollector(_fetcher().fetch_text).fetch("cobol") == []


def test_collector_identity():
    collector = WeWorkRemotelyCollector(_fetcher().fetch_text)
    assert collector.name == "weworkremotely"
    assert collector.query_based is True


def test_uses_the_text_path_not_json(monkeypatch):
    """The feed is XML: get_text must be used, so a JSON call would never happen."""
    replay.install(monkeypatch, replay.load("weworkremotely")["normal"])
    collector = WeWorkRemotelyCollector(_fetcher().fetch_text)
    assert len(collector.fetch("")) == 1
