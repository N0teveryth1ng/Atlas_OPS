import logging
from typing import ClassVar

import pytest
from pydantic import BaseModel

from atlas.llm import (
    DAILY_LIMIT_RETRY_AFTER_SECONDS,
    GroqDailyLimitReached,
    LLMClient,
    LLMError,
    _looks_like_daily_limit,
    _looks_like_json_mode_error,
    _retry_after_seconds,
    extract_json,
)


class Item(BaseModel):
    value: int


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kwargs):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return _Resp(response)


class _Chat:
    def __init__(self, responses):
        self.completions = _Completions(responses)


class FakeClient:
    def __init__(self, responses):
        self.chat = _Chat(responses)


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 1} done') == {"a": 1}


def test_call_json_retries_on_invalid_then_succeeds():
    fake = FakeClient(["not json", '{"value": 7}'])
    client = LLMClient(client=fake, max_validation_retries=3)
    item = client.call_json(schema=Item, system="s", user="u")
    assert item.value == 7
    assert fake.chat.completions.calls == 2


def test_call_json_raises_after_max_retries():
    fake = FakeClient(["nope"])
    client = LLMClient(client=fake, max_validation_retries=2)
    with pytest.raises(LLMError):
        client.call_json(schema=Item, system="s", user="u")
    assert fake.chat.completions.calls == 2


def test_extract_json_empty_response():
    with pytest.raises(ValueError):
        extract_json("")


def test_looks_like_json_mode_error():
    assert _looks_like_json_mode_error(Exception("response_format unsupported"))
    assert _looks_like_json_mode_error(Exception("json_object not allowed"))
    assert not _looks_like_json_mode_error(Exception("connection reset"))


def test_json_mode_fallback_on_unsupported_error(monkeypatch):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)
    fake = FakeClient([Exception("response_format is not supported"), '{"value": 5}'])
    client = LLMClient(client=fake)
    assert client.call_json(schema=Item, system="s", user="u").value == 5


def test_transport_failure_raises(monkeypatch):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)
    fake = FakeClient([Exception("boom")])
    client = LLMClient(client=fake, max_api_retries=2)
    with pytest.raises(LLMError):
        client.call_json(schema=Item, system="s", user="u")
    assert fake.chat.completions.calls == 2


def test_client_requires_api_key():
    with pytest.raises(LLMError):
        LLMClient(api_key=None)


def test_client_builds_groq_transport():
    client = LLMClient(api_key="test-key")
    assert client.default_model


class _RateLimitResponse:
    status_code = 429
    headers: ClassVar[dict[str, str]] = {"retry-after": "9"}


class _RateLimitError(Exception):
    def __init__(self, message: str = "429 Too Many Requests") -> None:
        super().__init__(message)
        self.response = _RateLimitResponse()


def test_retry_log_includes_status_retry_after_model_and_call_type(monkeypatch, caplog):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)
    fake = FakeClient([_RateLimitError(), '{"value": 3}'])
    client = LLMClient(client=fake, default_model="openai/gpt-oss-120b", max_api_retries=3)

    with caplog.at_level(logging.INFO, logger="atlas.llm"):
        item = client.call_json(schema=Item, system="s", user="u")

    assert item.value == 3
    retry_lines = [r.getMessage() for r in caplog.records if "LLM retry" in r.getMessage()]
    assert retry_lines, "expected an INFO retry log line"
    line = retry_lines[0]
    assert "status=429" in line
    assert "retry_after=9" in line
    assert "model=openai/gpt-oss-120b" in line
    assert "call_type=Item" in line


def test_429_notifies_wait_callback_with_retry_after(monkeypatch):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)
    fake = FakeClient([_RateLimitError(), '{"value": 3}'])
    client = LLMClient(client=fake, max_api_retries=3)
    waits: list[tuple[float, str]] = []
    client.on_wait = lambda seconds, reason: waits.append((seconds, reason))

    client.call_json(schema=Item, system="s", user="u")

    assert waits == [(9.0, "rate limit")]


def test_token_pacing_waits_on_remaining_tokens(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("atlas.llm.time.sleep", lambda seconds: sleeps.append(seconds))

    class _Msg2:
        def __init__(self, content):
            self.content = content

    class _Choice2:
        def __init__(self, content):
            self.message = _Msg2(content)

    class _Resp2:
        def __init__(self, content):
            self.choices = [_Choice2(content)]
            self.headers = {
                "x-ratelimit-remaining-tokens": "10",
                "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-reset-tokens": "3s",
            }

    class _Completions2:
        def create(self, **kwargs):
            return _Resp2('{"value": 1}')

    class _Chat2:
        def __init__(self):
            self.completions = _Completions2()

    class _Client2:
        def __init__(self):
            self.chat = _Chat2()

    client = LLMClient(client=_Client2())
    waits: list[tuple[float, str]] = []
    client.on_wait = lambda seconds, reason: waits.append((seconds, reason))

    client.call_json(schema=Item, system="s", user="u")  # learns remaining=10
    client.call_json(schema=Item, system="s", user="u")  # 10 < estimate -> pace

    assert (3.0, "token budget") in waits
    assert sleeps == [3.0]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12", 12.0),
        ("0s", 0.0),
        ("1m2.5s", 62.5),
        ("2m", 120.0),
        ("1.5s", 1.5),
        (None, None),
        ("", None),
        ("bogus", None),
    ],
)
def test_retry_after_seconds_parsing(value, expected):
    assert _retry_after_seconds(value) == expected


class _DailyLimitResponse:
    status_code = 429
    headers: ClassVar[dict[str, str]] = {"retry-after": "3600"}


class _DailyLimitError(Exception):
    def __init__(self, message: str = "429 Too Many Requests") -> None:
        super().__init__(message)
        self.response = _DailyLimitResponse()


def test_long_retry_after_raises_daily_limit_without_sleeping(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("atlas.llm.time.sleep", lambda seconds: sleeps.append(seconds))
    fake = FakeClient([_DailyLimitError(), '{"value": 3}'])
    client = LLMClient(client=fake, max_api_retries=3)

    with pytest.raises(GroqDailyLimitReached) as excinfo:
        client.call_json(schema=Item, system="s", user="u")

    # Sleeping for an hour would never finish the run, so the client must refuse.
    assert sleeps == []
    assert excinfo.value.reset_seconds == 3600.0
    assert "daily limit" in str(excinfo.value).lower()
    assert "1.0h" in str(excinfo.value)


def test_daily_limit_message_raises_even_without_long_retry_after(monkeypatch):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)
    exc = Exception(
        "Rate limit reached for openai/gpt-oss-120b on tokens per day (TPD): "
        "Limit 200000, Used 200000."
    )
    fake = FakeClient([exc])
    client = LLMClient(client=fake, max_api_retries=3)

    with pytest.raises(GroqDailyLimitReached):
        client.call_json(schema=Item, system="s", user="u")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tokens per day (TPD) exceeded", True),
        ("requests per day limit", True),
        ("Daily limit reached", True),
        ("Please try again in 5s", False),
        ("per day", True),
        ("rate limit reached, retry shortly", False),
    ],
)
def test_looks_like_daily_limit(text, expected):
    assert _looks_like_daily_limit(text) is expected


def test_short_429_still_retries_not_daily(monkeypatch):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)
    fake = FakeClient([_RateLimitError(), '{"value": 3}'])
    client = LLMClient(client=fake, max_api_retries=3)

    assert client.call_json(schema=Item, system="s", user="u").value == 3


def test_retry_after_threshold_is_boundary(monkeypatch):
    monkeypatch.setattr("atlas.llm.time.sleep", lambda *_: None)

    class _Resp:
        status_code = 429

        def __init__(self, retry_after: str) -> None:
            self.headers = {"retry-after": retry_after}

    class _Err(Exception):
        def __init__(self, retry_after: str) -> None:
            super().__init__("429 Too Many Requests")
            self.response = _Resp(retry_after)

    just_over = str(int(DAILY_LIMIT_RETRY_AFTER_SECONDS) + 1)
    with pytest.raises(GroqDailyLimitReached):
        LLMClient(client=FakeClient([_Err(just_over)]), max_api_retries=3).call_json(
            schema=Item, system="s", user="u"
        )

    with pytest.raises(LLMError) as excinfo:
        LLMClient(client=FakeClient([_Err("30")]), max_api_retries=2).call_json(
            schema=Item, system="s", user="u"
        )
    assert not isinstance(excinfo.value, GroqDailyLimitReached)


class _Usage:
    def __init__(self, total: int) -> None:
        self.prompt_tokens = total
        self.completion_tokens = 0
        self.total_tokens = total


class _UsageResp:
    def __init__(self, content: str, total: int) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage(total)


class _UsageCompletions:
    def create(self, **kwargs):
        return _UsageResp('{"value": 1}', 700)


class _UsageChat:
    def __init__(self) -> None:
        self.completions = _UsageCompletions()


class _UsageClient:
    def __init__(self) -> None:
        self.chat = _UsageChat()


def test_tokens_used_accumulates_across_calls():
    client = LLMClient(client=_UsageClient())
    assert client.tokens_used == 0

    client.call_json(schema=Item, system="s", user="u")
    client.call_json(schema=Item, system="s", user="u")

    assert client.tokens_used == 1400
