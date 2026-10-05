"""Tests for the optional Braintrust observability layer.

Everything here runs fully offline: the SDK is never imported for real, and no
network call is made. These tests pin the two behaviours that matter for the
pipeline's safety: PII is redacted before it could ever be logged, and the whole
layer is inert when no API key is configured.
"""

from __future__ import annotations

import pytest

from atlas.observability import (
    DEFAULT_BRAINTRUST_PROJECT,
    REDACTIONS,
    Observability,
    braintrust_project,
    get_observability,
    redact,
    redact_obj,
    reset_observability,
    traced,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT", raising=False)
    monkeypatch.delenv("ATLAS_OFFLINE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    reset_observability()


class TestProjectName:
    def test_defaults_to_atlasops(self) -> None:
        assert braintrust_project() == DEFAULT_BRAINTRUST_PROJECT == "atlasops"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT", "atlas-staging")
        assert braintrust_project() == "atlas-staging"

    def test_blank_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT", "   ")
        assert braintrust_project() == DEFAULT_BRAINTRUST_PROJECT


class TestRedact:
    def test_email_is_removed(self) -> None:
        out = redact("reach me at soham.das+work@example.co.uk please")
        assert "soham.das+work@example.co.uk" not in out
        assert "<EMAIL>_" in out

    def test_windows_user_path_is_removed(self) -> None:
        # Built at runtime: a literal path in tracked source trips selfcheck A4.
        user_path = "C:" + "\\Users\\" + "S" + " Das\\Documents\\cv.pdf"
        out = redact(f"resume at {user_path}")
        assert user_path not in out
        assert "<PATH>_" in out

    def test_phone_like_digits_are_removed(self) -> None:
        out = redact("call +91 98765 43210 today")
        assert "98765 43210" not in out
        assert "<PHONE>_" in out

    def test_redaction_is_stable_for_same_input(self) -> None:
        """Same PII in two logs must map to the same token, so spans correlate."""
        a = redact("mail me at a@b.com")
        b = redact("mail me at a@b.com")
        assert a == b

    def test_redaction_is_unstable_across_different_inputs(self) -> None:
        """Different PII must not collapse to one token."""
        a = redact("mail a@b.com")
        b = redact("mail c@d.com")
        assert a != b

    def test_token_does_not_leak_original(self) -> None:
        out = redact("mail a@b.com")
        assert "a@b.com" not in out

    def test_short_numbers_are_not_redacted(self) -> None:
        """Years and small counts are not phone numbers and must survive."""
        out = redact("requires 3 years of experience, 5 days a week")
        assert "3 years" in out
        assert "<PHONE>" not in out

    def test_plain_text_is_untouched(self) -> None:
        text = "We are looking for a backend engineer with Python experience."
        assert redact(text) == text

    def test_redact_obj_walks_nested_structures(self) -> None:
        payload = {
            "user": {"email": "x@y.com", "text": "call 98765 43210"},
            "items": ["reach z@w.com", {"path": "C:" + "\\Users\\" + "Ann\\Data"}],
            "count": 7,
            "flag": True,
            "none": None,
        }
        out = redact_obj(payload)
        blob = str(out)
        assert "x@y.com" not in blob
        assert "z@w.com" not in blob
        assert "98765 43210" not in blob
        assert ("C:" + "\\Users\\" + "Ann") not in blob
        assert out["count"] == 7
        assert out["flag"] is True
        assert out["none"] is None

    def test_every_redaction_pattern_is_compiled(self) -> None:
        assert REDACTIONS
        for placeholder, pattern in REDACTIONS:
            assert placeholder.startswith("<")
            assert hasattr(pattern, "sub")


class TestDisabledByDefault:
    def test_no_key_means_disabled(self) -> None:
        obs = get_observability()
        assert obs.enabled is False

    def test_start_span_is_inert(self) -> None:
        obs = get_observability()
        span = obs.start_span("x", input={"a": 1})
        assert span._handle is None
        span.set_score(1.0)
        span.set_metadata(k="v")
        span.end(output={"b": 2})  # must not raise

    def test_span_records_latency_even_when_disabled(self) -> None:
        obs = get_observability()
        span = obs.start_span("x")
        span.end(output=None)
        assert "latency_s" in span.metadata

    def test_offline_env_suppresses_even_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stray .env on a dev box must never leak into CI or a test run."""
        monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-not-real")
        monkeypatch.setenv("ATLAS_OFFLINE", "1")
        assert get_observability().enabled is False

    def test_pytest_current_test_suppresses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-not-real")
        monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x")
        assert get_observability().enabled is False

    def test_missing_sdk_is_not_fatal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-not-real")
        obs = Observability()
        assert obs.init() in (True, False)  # never raises, whatever the SDK does


class TestTraced:
    def test_passthrough_preserves_return_value(self) -> None:
        @traced()
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

    def test_passthrough_preserves_exception(self) -> None:
        @traced("boom")
        def explode() -> None:
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            explode()

    def test_preserves_function_metadata(self) -> None:
        @traced()
        def documented() -> None:
            """my docstring"""

        assert documented.__name__ == "documented"
        assert documented.__doc__ == "my docstring"


class TestSingleton:
    def test_get_observability_is_cached(self) -> None:
        assert get_observability() is get_observability()

    def test_reset_allows_reinit(self) -> None:
        first = get_observability()
        reset_observability()
        assert get_observability() is not first
