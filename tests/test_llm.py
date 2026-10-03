import pytest
from pydantic import BaseModel

from atlas.llm import LLMClient, LLMError, _looks_like_json_mode_error, extract_json


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
