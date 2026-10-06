"""Replay recorded collector fixtures without touching the network.

Every ``tests/fixtures/<source>.json`` file holds the five response shapes each
collector must survive — ``normal``, ``empty``, ``malformed``, ``http_429`` and
``http_500``. A case is a whole HTTP response (status + headers + body), so the
real :class:`~atlas.collectors.base.HttpFetcher` retry and ``Retry-After`` code
paths are exercised rather than stubbed out.

``install`` patches :func:`atlas.collectors.base._http_get`, the single request
primitive every path goes through, and returns the log of requested URLs.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from atlas.collectors import base

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: The five response shapes every source fixture must cover, in test order.
CASE_NAMES = ("normal", "empty", "malformed", "http_429", "http_500")


@dataclass(frozen=True)
class Recorded:
    """One recorded (or synthetic) HTTP response for a collector."""

    source: str
    case: str
    status: int
    headers: dict[str, str]
    body: Any
    url: str
    provenance: str

    def response(self, url: str | None = None) -> httpx.Response:
        """Rebuild this case as an ``httpx.Response`` bound to ``url``."""
        request = httpx.Request("GET", url or self.url)
        headers = dict(self.headers)
        if isinstance(self.body, str):
            return httpx.Response(self.status, headers=headers, text=self.body, request=request)
        return httpx.Response(self.status, headers=headers, json=self.body, request=request)


def fixture_path(source: str) -> Path:
    return FIXTURES_DIR / f"{source}.json"


def load(source: str) -> dict[str, Recorded]:
    """Load every recorded case for ``source``, keyed by case name."""
    raw = json.loads(fixture_path(source).read_text(encoding="utf-8"))
    cases: dict[str, Recorded] = {}
    for name, case in (raw.get("cases") or {}).items():
        cases[name] = Recorded(
            source=raw.get("source", source),
            case=name,
            status=int(case.get("status", 200)),
            headers={str(k): str(v) for k, v in (case.get("headers") or {}).items()},
            body=case.get("body"),
            url=raw.get("url", ""),
            provenance=case.get("provenance", "recorded"),
        )
    return cases


def install_sequence(
    monkeypatch,
    cases: Sequence[Recorded],
    *,
    repeat_last: bool = True,
) -> list[str]:
    """Replay cases in request order; returns the log of requested URLs.

    With ``repeat_last=True`` the final case answers every later request, which is
    how the "always 429"/"always 500" fixtures reach ``max_attempts``.
    """
    if not cases:
        raise ValueError("install_sequence needs at least one recorded case")
    log: list[str] = []
    served = 0

    def fake_get(url: str, params: dict | None, *, timeout: float) -> httpx.Response:
        nonlocal served
        log.append(url)
        if served >= len(cases) and not repeat_last:
            raise AssertionError(f"unexpected extra request #{served} to {url}")
        recorded = cases[min(served, len(cases) - 1)]
        served += 1
        return recorded.response(url)

    monkeypatch.setattr(base, "_http_get", fake_get)
    return log


def install(monkeypatch, recorded: Recorded) -> list[str]:
    """Replay one recorded case for every request; returns the request log."""
    return install_sequence(monkeypatch, [recorded])


def policy(**overrides: Any) -> base.HttpPolicy:
    """A policy with the sleeps removed, so retry tests cost no wall-clock time."""
    settings: dict[str, Any] = {"sleep_seconds": 0.0}
    settings.update(overrides)
    return base.HttpPolicy(**settings)
