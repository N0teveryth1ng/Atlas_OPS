"""Tests for the shared HTTP plumbing in atlas.collectors.base.

No network: `_http_get` is patched so the real retry / Retry-After / parse paths
still run against a stored response.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from tenacity import RetryCallState

from atlas.collectors import base
from tests import replay
from tests.replay import Recorded


def _case(status: int, body: Any, headers: dict[str, str] | None = None) -> Recorded:
    return Recorded(
        source="unit",
        case="normal",
        status=status,
        headers=headers or {},
        body=body,
        url="https://unit.test/api",
        provenance="synthetic",
    )


# --- Retry-After / date parsing ------------------------------------------------


def test_parse_retry_after_delta_seconds():
    assert base.parse_retry_after("120") == 120.0
    assert base.parse_retry_after(" 7 ") == 7.0


def test_parse_retry_after_http_date():
    now = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
    assert base.parse_retry_after("Mon, 05 Oct 2026 12:01:30 GMT", now=now) == 90.0


def test_parse_retry_after_clamps_past_dates_to_zero():
    now = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)
    assert base.parse_retry_after("Mon, 05 Oct 2026 11:00:00 GMT", now=now) == 0.0


def test_parse_retry_after_rejects_garbage():
    assert base.parse_retry_after(None) is None
    assert base.parse_retry_after("") is None
    assert base.parse_retry_after("soon-ish") is None


def test_parse_http_date_handles_rss_pubdate():
    stamp = base.parse_http_date("Tue, 30 Sep 2025 15:37:54 +0000")
    assert stamp == datetime(2025, 9, 30, 15, 37, 54, tzinfo=UTC)


def test_parse_http_date_is_naive_input_tolerant():
    assert base.parse_http_date("nonsense") is None
    assert base.parse_http_date(None) is None


def test_parse_datetime_accepts_epoch_iso_and_millis():
    assert base.parse_datetime(1791243604) == datetime.fromtimestamp(1791243604, tz=UTC)
    assert base.parse_datetime(1791243604000) == datetime.fromtimestamp(1791243604, tz=UTC)
    assert base.parse_datetime("2026-10-05T09:00:00Z") == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    assert base.parse_datetime("2026-10-05T09:00:00") == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    assert base.parse_datetime("not a date") is None
    assert base.parse_datetime(None) is None


def test_wait_seconds_prefers_retry_after():
    wait = base._wait_seconds(base.HttpPolicy())
    state = _state_with(base.RetryableStatusError(429, retry_after=7.0))
    assert wait(state) == 7.0


def test_wait_seconds_caps_retry_after():
    wait = base._wait_seconds(base.HttpPolicy(max_retry_after=10.0))
    state = _state_with(base.RetryableStatusError(503, retry_after=3600.0))
    assert wait(state) == 10.0


def test_wait_seconds_falls_back_to_exponential():
    wait = base._wait_seconds(base.HttpPolicy(backoff_multiplier=2.0, backoff_max=8.0))
    assert wait(_state_with(base.RetryableStatusError(500), attempt_number=2)) == 4.0
    assert wait(_state_with(base.RetryableStatusError(500), attempt_number=3)) == 8.0
    assert wait(_state_with(base.RetryableStatusError(500), attempt_number=9)) == 8.0


def test_wait_seconds_uses_backoff_for_plain_http_errors():
    """Matches the legacy backoff: 2s after the first failure, 4s after the second."""
    wait = base._wait_seconds(base.HttpPolicy())
    assert wait(_state_with(httpx.ConnectError("boom"), attempt_number=2)) == 2.0
    assert wait(_state_with(httpx.ConnectError("boom"), attempt_number=3)) == 4.0


# --- HttpFetcher ---------------------------------------------------------------


def test_fetch_json_returns_decoded_body(monkeypatch):
    log = replay.install(monkeypatch, _case(200, {"jobs": [1, 2]}))
    assert base.HttpFetcher(replay.policy()).fetch_json("https://unit.test/api") == {"jobs": [1, 2]}
    assert log == ["https://unit.test/api"]


def test_fetcher_is_callable_for_get_json_injection(monkeypatch):
    replay.install(monkeypatch, _case(200, {"ok": True}))
    get_json = base.HttpFetcher(replay.policy())
    assert get_json("https://unit.test/api", {"q": "python"}) == {"ok": True}


def test_fetch_json_passes_params_and_timeout(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_get(url: str, params: dict | None, *, timeout: float) -> httpx.Response:
        seen.update(url=url, params=params, timeout=timeout)
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(base, "_http_get", fake_get)
    base.HttpFetcher(base.HttpPolicy(timeout=7.5)).fetch_json("https://unit.test/api", {"q": "go"})
    assert seen == {"url": "https://unit.test/api", "params": {"q": "go"}, "timeout": 7.5}


def test_request_sets_identifying_user_agent(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_get(url: str, params: dict, *, timeout: float, headers: dict) -> httpx.Response:
        seen["headers"] = headers
        return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(base.httpx, "get", fake_get)
    base.HttpFetcher(base.HttpPolicy()).fetch_json("https://unit.test/api")
    assert seen["headers"]["User-Agent"] == base.USER_AGENT


def test_fetch_text_returns_raw_body(monkeypatch):
    replay.install(monkeypatch, _case(200, "<rss><item/></rss>"))
    assert base.HttpFetcher(replay.policy()).fetch_text("https://unit.test/rss") == (
        "<rss><item/></rss>"
    )


def test_429_is_retried_then_recovers(monkeypatch):
    log = replay.install_sequence(
        monkeypatch,
        [
            _case(429, {"error": "rate limited"}, {"Retry-After": "60"}),
            _case(200, {"jobs": ["recovered"]}),
        ],
        repeat_last=False,
    )
    fetcher = base.HttpFetcher(replay.policy())
    assert fetcher.fetch_json("https://unit.test/api") == {"jobs": ["recovered"]}
    assert len(log) == 2


def test_429_gives_up_after_max_attempts(monkeypatch):
    fetcher = base.HttpFetcher(replay.policy(max_attempts=3))
    log = replay.install(monkeypatch, _case(429, {}, {"Retry-After": "60"}))
    with pytest.raises(base.RetryableStatusError) as caught:
        fetcher.fetch_json("https://unit.test/api")
    assert caught.value.status_code == 429
    assert caught.value.retry_after == 60.0
    assert len(log) == 3


def test_500_is_retried_then_gives_up(monkeypatch):
    fetcher = base.HttpFetcher(replay.policy(max_attempts=2))
    log = replay.install(monkeypatch, _case(500, {"error": "boom"}))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher.fetch_json("https://unit.test/api")
    assert len(log) == 2


def test_404_is_not_retried(monkeypatch):
    """A 4xx is a permanent answer: retrying it just burns the budget."""
    log = replay.install(monkeypatch, _case(404, {"error": "gone"}))
    with pytest.raises(httpx.HTTPStatusError):
        base.HttpFetcher(replay.policy()).fetch_json("https://unit.test/api")
    assert len(log) == 1


def test_malformed_json_raises_and_survives_safe_fetch(monkeypatch):
    replay.install(monkeypatch, _case(200, "<html>maintenance</html>"))
    collector = _StubCollector(base.HttpFetcher(replay.policy()))
    assert collector.safe_fetch("python") == []


def test_safe_fetch_returns_empty_on_retry_exhaustion(monkeypatch):
    replay.install(monkeypatch, _case(429, {}, {"Retry-After": "1"}))
    collector = _StubCollector(base.HttpFetcher(replay.policy(max_attempts=1)))
    assert collector.safe_fetch("python") == []


def test_sleep_override_short_circuits_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", slept.append)
    fetcher = base.HttpFetcher(base.HttpPolicy(max_attempts=3, backoff_min=30.0, sleep_seconds=0.0))
    replay.install(monkeypatch, _case(500, {}))
    with pytest.raises(httpx.HTTPStatusError):
        fetcher.fetch_json("https://unit.test/api")
    assert slept == [0.0, 0.0]


def test_default_policy_matches_legacy_retry_shape():
    policy = base.HttpPolicy()
    assert policy.max_attempts == base.DEFAULT_ATTEMPTS == 3
    assert (policy.timeout, policy.backoff_min, policy.backoff_max) == (20.0, 1.0, 8.0)
    assert base.RETRYABLE_STATUS == frozenset({429, 503})


def test_should_retry_matrix():
    """429/503 retry, 5xx retries, 404 and friends do not."""
    assert base._should_retry(base.RetryableStatusError(429, retry_after=None))
    assert base._should_retry(_status_error(503))
    assert base._should_retry(_status_error(500))
    assert base._should_retry(httpx.ConnectError("no route"))
    assert not base._should_retry(_status_error(404))
    assert not base._should_retry(_status_error(403))
    assert not base._should_retry(ValueError("bad payload"))


def test_default_get_json_delegates_to_legacy_helper(monkeypatch):
    calls: list[tuple[str, dict | None]] = []

    def fake_helper(url: str, params: dict | None = None, **kwargs: Any) -> Any:
        calls.append((url, params))
        return {"legacy": True}

    monkeypatch.setattr(base, "_http_get_json", fake_helper)
    assert base.default_get_json("https://unit.test/api", {"x": 1}) == {"legacy": True}
    assert calls == [("https://unit.test/api", {"x": 1})]


# --- Collector base -----------------------------------------------------------


class _StubCollector(base.Collector):
    name = "stub"
    query_based = False

    def __init__(self, get_json: base.GetJson) -> None:
        super().__init__(get_json)

    def fetch(self, query: str) -> list[Any]:
        return [self.get_json("https://unit.test/api", None)]


def test_collector_defaults_get_text_to_a_retrying_fetcher():
    collector = _StubCollector(lambda url, params: None)
    assert callable(collector.get_text)


def _state_with(error: Exception, *, attempt_number: int = 2) -> RetryCallState:
    state = RetryCallState(cast(Any, None), cast(Any, None), (), {})
    state.attempt_number = attempt_number
    state.set_exception((type(error), error, cast(Any, None)))
    return state


def test_fixture_case_names_match_the_documented_contract():
    assert replay.CASE_NAMES == ("normal", "empty", "malformed", "http_429", "http_500")
    assert replay.fixture_path("himalayas").name == "himalayas.json"


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://unit.test/api")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)
