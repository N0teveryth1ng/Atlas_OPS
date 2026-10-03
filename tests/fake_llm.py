"""Shared fake Groq-like client for tests (no network)."""

from __future__ import annotations

import json

from atlas.llm import LLMClient


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
    def __init__(self, handler):
        self._handler = handler
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Resp(self._handler(kwargs))


class _Chat:
    def __init__(self, handler):
        self.completions = _Completions(handler)


class FakeClient:
    def __init__(self, handler):
        self.chat = _Chat(handler)


def make_client(*, parsed=None, verdict=None, verifier=None):
    """Route canned payloads by inspecting the system prompt."""

    def handler(kwargs):
        system = kwargs["messages"][0]["content"]
        if "adversarial reviewer" in system:
            payload = verifier
        elif "structured requirements" in system:
            payload = parsed
        else:
            payload = verdict
        if payload is None:
            raise AssertionError(f"no canned response for system prompt: {system[:60]!r}")
        return json.dumps(payload)

    return LLMClient(client=FakeClient(handler))
