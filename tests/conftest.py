"""Test isolation: deterministic environment regardless of the dev machine's state.

An autouse fixture clears app-critical env vars before every test so no test can
leak state into later tests and no developer's own environment can change a
test's result. Tests that need a value set it explicitly with monkeypatch.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("ATLAS_DB", raising=False)
