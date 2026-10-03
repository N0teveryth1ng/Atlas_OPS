"""Shared fake Groq-like client for tests (no network)."""

from __future__ import annotations

import json

from atlas.llm import LLMClient

_BENIGN_PROJECT_RELEVANCE = {"per_project": [], "overall": 50}
_BENIGN_CRITIC = {"propose_veto": False, "strongest_reason": None, "reasons_against": []}


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


def make_client(
    *,
    parsed=None,
    verdict=None,
    verifier=None,
    project_relevance=None,
    critic=None,
):
    """Route canned payloads by inspecting the system prompt.

    Missing LLM dimensions default to benign values so existing tests that only
    exercise parse/evaluate/verify keep working without opting in.
    """

    project = project_relevance if project_relevance is not None else _BENIGN_PROJECT_RELEVANCE
    critic_payload = critic if critic is not None else _BENIGN_CRITIC

    def handler(kwargs):
        system = kwargs["messages"][0]["content"]
        if "adversarial reviewer" in system:
            payload = verifier
        elif "structured requirements" in system:
            payload = parsed
        elif "project-to-role relevance analyst" in system:
            payload = project
        elif "adversarial final decision critic" in system:
            payload = critic_payload
        else:
            payload = verdict
        if payload is None:
            raise AssertionError(f"no canned response for system prompt: {system[:60]!r}")
        return json.dumps(payload)

    return LLMClient(client=FakeClient(handler))
