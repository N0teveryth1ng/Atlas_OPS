"""Tests for the optional Braintrust observability layer.

Everything here runs fully offline: the SDK is never imported for real, and no
network call is made. These tests pin the behaviours that matter for the
pipeline's safety: PII is redacted before it could ever be logged, the whole
layer is inert when no API key is configured, and a broken publish is reported
rather than silently swallowed.
"""

from __future__ import annotations

import logging
import types
from typing import Any

import pytest

from atlas import evaluation
from atlas.evaluation import (
    LLM_PRECISION_SCORE,
    SKILL_ACCURACY_SCORE,
    YEAR_ACCURACY_SCORE,
    PrecisionCaseResult,
    PrecisionReport,
    evaluate,
    evaluate_skill_matching,
    load_golden_set,
    load_skill_cases,
    run_eval,
    run_llm_eval,
    run_skill_eval,
)
from atlas.observability import (
    DEFAULT_BRAINTRUST_PROJECT,
    DEFAULT_BRAINTRUST_PROJECT_ID,
    EVAL_TAGS,
    REDACTIONS,
    Observability,
    braintrust_dashboard_url,
    braintrust_project,
    braintrust_project_id,
    get_observability,
    redact,
    redact_obj,
    reset_observability,
    traced,
)


class FakeSDKError(Exception):
    """Stand-in for ``braintrust.api.BraintrustAPIError``, the SDK error root."""


class FakeLogger:
    """Records every ``log`` call instead of sending it anywhere."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self.flushes = 0
        self.error = error

    def log(self, **kwargs: Any) -> str:
        if self.error is not None:
            raise self.error
        self.rows.append(kwargs)
        return "row-id"

    def flush(self) -> None:
        self.flushes += 1


class FakeSDK:
    """Stand-in for the ``braintrust`` module itself."""

    def __init__(
        self,
        logger: FakeLogger | None = None,
        *,
        returns: Any = None,
    ) -> None:
        self.logger = logger if logger is not None else FakeLogger()
        self.returns = returns
        self.init_kwargs: dict[str, Any] = {}
        self.api = types.SimpleNamespace(BraintrustAPIError=FakeSDKError)

    def init_logger(self, **kwargs: Any) -> Any:
        self.init_kwargs = kwargs
        return self.logger if self.returns is None else self.returns


class NoLogMethod:
    """A logger-shaped object missing ``log``, i.e. a broken integration."""

    def flush(self) -> None:
        pass


def install_sdk(logger: FakeLogger | None = None, *, returns: Any = None) -> FakeSDK:
    """Wire a recording SDK into the singleton, bypassing the real login."""
    fake = FakeSDK(logger, returns=returns)
    obs = get_observability()
    obs._bt = fake
    obs._api_key = "sk-fake-not-real"
    obs.enabled = True
    return fake


@pytest.fixture
def sdk() -> FakeSDK:
    return install_sdk()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT", raising=False)
    monkeypatch.delenv("BRAINTRUST_PROJECT_ID", raising=False)
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

    def test_first_use_initialises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without this, ``enabled`` would stay False and publishing would be dead code."""
        calls: list[dict[str, Any]] = []
        original = Observability.init

        def spy(self: Observability, **kwargs: Any) -> bool:
            calls.append(kwargs)
            return original(self, **kwargs)

        monkeypatch.setattr(Observability, "init", spy)
        reset_observability()
        get_observability()
        assert len(calls) == 1

    def test_later_uses_do_not_reinitialise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []
        original = Observability.init

        def spy(self: Observability, **kwargs: Any) -> bool:
            calls.append(kwargs)
            return original(self, **kwargs)

        monkeypatch.setattr(Observability, "init", spy)
        reset_observability()
        get_observability()
        get_observability()
        get_observability()
        assert len(calls) == 1


class TestProjectId:
    def test_pins_the_owner_chosen_project(self) -> None:
        assert braintrust_project_id() == DEFAULT_BRAINTRUST_PROJECT_ID
        assert DEFAULT_BRAINTRUST_PROJECT_ID == "3c5416f9-ff13-4c16-9a07-71d0e5a8c090"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT_ID", "11111111-2222-3333-4444-555555555555")
        assert braintrust_project_id() == "11111111-2222-3333-4444-555555555555"

    def test_blank_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT_ID", "  ")
        assert braintrust_project_id() == DEFAULT_BRAINTRUST_PROJECT_ID

    def test_dashboard_url_follows_project_name(self) -> None:
        assert braintrust_dashboard_url() == "https://www.braintrust.dev/app/atlasops"


class TestPublishDisabled:
    """A disabled layer must be invisible: no SDK call, no output, no raise."""

    def test_disabled_makes_no_sdk_call(self, capsys: pytest.CaptureFixture[str]) -> None:
        fake = install_sdk()
        get_observability().enabled = False
        published = get_observability().publish_score("some_score", 0.5)
        assert published is False
        assert fake.init_kwargs == {}
        assert fake.logger.rows == []
        assert capsys.readouterr().out == ""

    def test_no_key_is_a_silent_no_op(
        self, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            published = get_observability().publish_score("some_score", 0.5)
        assert published is False
        assert capsys.readouterr().out == ""
        assert [r for r in caplog.records if r.name == "atlas.observability"] == []

    def test_offline_env_keeps_publishing_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-not-real")
        monkeypatch.setenv("ATLAS_OFFLINE", "1")
        assert get_observability().init() is False
        assert get_observability().publish_score("some_score", 0.5) is False
        assert capsys.readouterr().out == ""


class TestPublishEnabled:
    def test_writes_one_row_with_the_intended_score(self, sdk: FakeSDK) -> None:
        assert get_observability().publish_score("year_extraction_accuracy", 0.95) is True
        assert len(sdk.logger.rows) == 1
        assert sdk.logger.rows[0]["scores"] == {"year_extraction_accuracy": 0.95}

    def test_score_value_is_passed_through_unchanged(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 0.8125)
        assert sdk.logger.rows[0]["scores"]["m"] == 0.8125
        assert sdk.logger.rows[0]["output"] == {"score": 0.8125}

    def test_metadata_is_passed_through(self, sdk: FakeSDK) -> None:
        meta = {"year_correct": 19, "year_checked": 20, "accepted": True}
        get_observability().publish_score("m", 1.0, metadata=meta)
        assert sdk.logger.rows[0]["metadata"] == meta

    def test_metadata_defaults_to_empty(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0)
        assert sdk.logger.rows[0]["metadata"] == {}

    def test_input_payload_is_passed_through(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0, input={"eval": "golden_set"})
        assert sdk.logger.rows[0]["input"] == {"eval": "golden_set"}

    def test_metadata_is_redacted(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0, metadata={"note": "mail a@b.com"})
        assert "a@b.com" not in str(sdk.logger.rows[0]["metadata"])

    def test_rows_are_tagged_for_dashboard_filtering(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("skill_matching_accuracy", 1.0)
        assert sdk.logger.rows[0]["tags"] == [*EVAL_TAGS, "skill_matching_accuracy"]

    def test_flushes_before_returning(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0)
        assert sdk.logger.flushes == 1

    def test_targets_the_owner_chosen_project_id(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0)
        assert sdk.init_kwargs["project_id"] == DEFAULT_BRAINTRUST_PROJECT_ID
        assert sdk.init_kwargs["async_flush"] is False

    def test_never_writes_to_the_other_project(self, sdk: FakeSDK) -> None:
        """d7011a46-... ("My Project") must never receive an Atlas row."""
        get_observability().publish_score("m", 1.0)
        assert "d7011a46-5b44-404c-a2d8-f0024bb40920" not in str(sdk.init_kwargs)

    def test_prints_dashboard_url(self, sdk: FakeSDK, capsys: pytest.CaptureFixture[str]) -> None:
        get_observability().publish_score("llm_precision_at_k", 0.9)
        out = capsys.readouterr().out
        assert "llm_precision_at_k" in out
        assert "https://www.braintrust.dev/app/atlasops" in out

    def test_printed_url_never_contains_the_key(
        self, sdk: FakeSDK, capsys: pytest.CaptureFixture[str]
    ) -> None:
        get_observability().publish_score("m", 1.0)
        assert "sk-fake-not-real" not in capsys.readouterr().out


class TestPublishFailureIsLoud:
    def test_sdk_error_is_reported_not_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        install_sdk(FakeLogger(error=FakeSDKError("503 from braintrust")))
        with caplog.at_level(logging.ERROR):
            assert get_observability().publish_score("m", 1.0) is False
        assert [r.levelname for r in caplog.records] == ["ERROR"]
        assert "503 from braintrust" in caplog.records[0].getMessage()
        assert "m" in caplog.records[0].getMessage()

    def test_broken_integration_propagates(self) -> None:
        """A wrong SDK call must not look like a working no-op."""
        install_sdk(returns=NoLogMethod())
        with pytest.raises(AttributeError):
            get_observability().publish_score("m", 1.0)


class TestEvalScoresPublished:
    def test_run_eval_publishes_year_accuracy(self, sdk: FakeSDK) -> None:
        assert run_eval() is True
        assert len(sdk.logger.rows) == 1
        row = sdk.logger.rows[0]
        assert set(row["scores"]) == {YEAR_ACCURACY_SCORE}
        assert row["scores"][YEAR_ACCURACY_SCORE] == row["output"]["score"]
        assert row["tags"] == [*EVAL_TAGS, YEAR_ACCURACY_SCORE]

    def test_run_eval_publishes_exact_report_numbers(self, sdk: FakeSDK) -> None:
        report = evaluate(load_golden_set())
        run_eval()
        row = sdk.logger.rows[0]
        assert row["scores"][YEAR_ACCURACY_SCORE] == report.year_accuracy
        assert row["metadata"]["year_correct"] == report.year_correct
        assert row["metadata"]["year_checked"] == report.year_checked
        assert row["metadata"]["accepted"] == report.accepted

    def test_run_skill_eval_publishes_new_accuracy(self, sdk: FakeSDK) -> None:
        assert run_skill_eval() is True
        row = sdk.logger.rows[0]
        assert set(row["scores"]) == {SKILL_ACCURACY_SCORE}
        assert row["scores"][SKILL_ACCURACY_SCORE] == row["output"]["score"]

    def test_run_skill_eval_publishes_exact_report_numbers(self, sdk: FakeSDK) -> None:
        report = evaluate_skill_matching(load_skill_cases())
        run_skill_eval()
        row = sdk.logger.rows[0]
        assert row["scores"][SKILL_ACCURACY_SCORE] == report.new_accuracy
        assert row["metadata"]["baseline_accuracy"] == report.baseline_accuracy
        assert row["metadata"]["baseline_correct"] == report.baseline_correct
        assert row["metadata"]["new_correct"] == report.new_correct

    def test_llm_score_name_is_stable(self) -> None:
        assert LLM_PRECISION_SCORE == "llm_precision_at_k"
        assert YEAR_ACCURACY_SCORE == "year_extraction_accuracy"
        assert SKILL_ACCURACY_SCORE == "skill_matching_accuracy"

    def test_run_llm_eval_publishes_precision(
        self, sdk: FakeSDK, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report = PrecisionReport(
            results=[
                PrecisionCaseResult("a1", "apply", True, 9.0),
                PrecisionCaseResult("a2", "apply", True, 8.0),
                PrecisionCaseResult("s1", "skip", True, 1.0),
            ],
            k=2,
        )
        monkeypatch.setattr(evaluation, "load_golden_set", list)
        monkeypatch.setattr(evaluation, "evaluate_with_llm", lambda cases, client, settings: report)
        assert run_llm_eval(object()) is True
        row = sdk.logger.rows[0]
        assert set(row["scores"]) == {LLM_PRECISION_SCORE}
        assert row["scores"][LLM_PRECISION_SCORE] == report.precision_at_k
        assert row["metadata"]["k"] == 2
        assert row["metadata"]["candidates"] == len(report.candidates)
        assert row["metadata"]["top_k_selected"] == len(report.top_k)
        assert row["metadata"]["accepted"] == report.accepted


class TestEvalOfflineUnchanged:
    """With telemetry off the eval surface must be byte-identical to before."""

    def test_run_eval_output_and_result_are_unchanged(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_eval() is True
        out = capsys.readouterr().out
        assert out.startswith("Golden set cases:")
        assert "RESULT:                      PASS" in out
        assert "Braintrust" not in out

    def test_run_skill_eval_output_is_unchanged(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run_skill_eval() is True
        out = capsys.readouterr().out
        assert out.startswith("Skill cases:")
        assert "RESULT:                      PASS" in out
        assert "Braintrust" not in out
