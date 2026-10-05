"""Tests for the optional Braintrust observability layer.

Everything here runs fully offline: the SDK is never imported for real, and no
network call is made. These tests pin the behaviours that matter for the
pipeline's safety: an explicit allowlist decides what may leave the process, a
realistic run's serialized payloads carry no resume, profile or contact detail,
prompt text is never traced, and every tracing fault degrades to a warning
instead of failing the run that produced it.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from fake_llm import make_client
from pydantic import BaseModel

from atlas import evaluation, observability
from atlas.config import get_settings
from atlas.db import connect, init_db, save_profile, start_run, upsert_job
from atlas.evaluation import (
    DECISION_AGREEMENT_SCORE,
    EVAL_EXPERIMENT,
    LLM_PRECISION_SCORE,
    SENIORITY_PASSTHROUGH_SCORE,
    SKILL_ACCURACY_SCORE,
    YEAR_ACCURACY_SCORE,
    EvalReport,
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
from atlas.llm import LLMClient, LLMError
from atlas.observability import (
    ALLOWED_PAYLOAD_KEYS,
    ALLOWED_URL_FIELDS,
    DEFAULT_BRAINTRUST_ORG,
    DEFAULT_BRAINTRUST_PROJECT,
    DEFAULT_BRAINTRUST_PROJECT_ID,
    EVAL_TAGS,
    REDACTIONS,
    Observability,
    allowlist_payload,
    braintrust_dashboard_url,
    braintrust_org,
    braintrust_project,
    braintrust_project_id,
    get_observability,
    prompt_fingerprint,
    redact,
    redact_obj,
    reset_observability,
    traced,
    traced_stage,
)
from atlas.pipeline import _job_fields, process_job, run_pipeline
from atlas.schemas import CandidateProfile, Job, Proficiency, Skill


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


class FakeSpan:
    """Records how a span was started, logged and ended.

    ``log(**event)`` followed by ``end(end_time=None)`` mirrors the real SDK,
    whose ``Span.end`` accepts no event fields. Recording that shape here is
    deliberate: a fake with a looser ``end(**kwargs)`` lets a call the real
    client would reject pass in tests.
    """

    def __init__(
        self, name: str, start_kwargs: dict[str, Any], error: BaseException | None
    ) -> None:
        self.name = name
        self.start_kwargs = start_kwargs
        self.events: list[dict[str, Any]] = []
        self.end_kwargs: dict[str, Any] | None = None
        self.error = error

    def log(self, **event: Any) -> None:
        if self.error is not None:
            raise self.error
        self.events.append(event)

    def end(self, end_time: float | None = None) -> float:
        if self.error is not None:
            raise self.error
        self.end_kwargs = {"end_time": end_time}
        return end_time or 0.0

    @property
    def ended(self) -> bool:
        return self.end_kwargs is not None

    def metadata(self) -> dict[str, Any]:
        """Metadata of this span's last logged event, where end data lands."""
        return self.events[-1]["metadata"]

    def sent(self) -> str:
        """Everything this span put on the wire, as one string."""
        return f"{self.start_kwargs}{self.events}"


class FakeSDK:
    """Stand-in for the ``braintrust`` module itself."""

    def __init__(
        self,
        logger: FakeLogger | None = None,
        *,
        returns: Any = None,
        span_error: BaseException | None = None,
        span_end_error: BaseException | None = None,
    ) -> None:
        self.logger = logger if logger is not None else FakeLogger()
        self.returns = returns
        self.span_error = span_error
        self.span_end_error = span_end_error
        self.spans: list[FakeSpan] = []
        self.init_kwargs: dict[str, Any] = {}
        self.login_kwargs: dict[str, Any] = {}
        self.api = types.SimpleNamespace(BraintrustAPIError=FakeSDKError)

    def login(self, **kwargs: Any) -> None:
        self.login_kwargs = kwargs

    def init_logger(self, **kwargs: Any) -> Any:
        self.init_kwargs = kwargs
        return self.logger if self.returns is None else self.returns

    def start_span(self, *, name: str, **kwargs: Any) -> FakeSpan:
        if self.span_error is not None:
            raise self.span_error
        span = FakeSpan(name, {"name": name, **kwargs}, self.span_end_error)
        self.spans.append(span)
        return span


class NoLogMethod:
    """A logger-shaped object missing ``log``, i.e. a broken integration."""


def _raise_logger_error(**_: Any) -> Any:
    raise FakeSDKError("project logger unavailable")


def install_sdk(
    logger: FakeLogger | None = None,
    *,
    returns: Any = None,
    span_error: BaseException | None = None,
    span_end_error: BaseException | None = None,
) -> FakeSDK:
    """Wire a recording SDK into the singleton, bypassing the real login."""
    fake = FakeSDK(logger, returns=returns, span_error=span_error, span_end_error=span_end_error)
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
    monkeypatch.delenv("BRAINTRUST_ORG", raising=False)
    monkeypatch.delenv("ATLAS_OFFLINE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    reset_observability()


class TestProjectName:
    def test_defaults_to_the_owner_chosen_project(self) -> None:
        assert braintrust_project() == DEFAULT_BRAINTRUST_PROJECT == "My Project"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT", "atlas-staging")
        assert braintrust_project() == "atlas-staging"

    def test_blank_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT", "   ")
        assert braintrust_project() == DEFAULT_BRAINTRUST_PROJECT


class TestOrgName:
    def test_defaults_to_the_owning_org(self) -> None:
        assert braintrust_org() == DEFAULT_BRAINTRUST_ORG == "atlasops"

    def test_org_is_not_the_project_name(self) -> None:
        """Login needs the org; pointing it at the project name breaks login."""
        assert braintrust_org() != braintrust_project()

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_ORG", "other-org")
        assert braintrust_org() == "other-org"


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
        assert DEFAULT_BRAINTRUST_PROJECT_ID == "d7011a46-5b44-404c-a2d8-f0024bb40920"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT_ID", "11111111-2222-3333-4444-555555555555")
        assert braintrust_project_id() == "11111111-2222-3333-4444-555555555555"

    def test_blank_env_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT_ID", "  ")
        assert braintrust_project_id() == DEFAULT_BRAINTRUST_PROJECT_ID

    def test_dashboard_url_follows_project_name(self) -> None:
        # Encoded: the owner-chosen project name is "My Project", and a raw
        # space would make the printed link unopenable.
        assert braintrust_dashboard_url() == "https://www.braintrust.dev/app/My%20Project"

    def test_dashboard_url_follows_the_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BRAINTRUST_PROJECT", "atlas-staging")
        assert braintrust_dashboard_url() == "https://www.braintrust.dev/app/atlas-staging"


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

    def test_never_writes_to_the_retired_project(self, sdk: FakeSDK) -> None:
        """3c5416f9-... is the old target and must no longer receive writes."""
        get_observability().publish_score("m", 1.0)
        assert sdk.init_kwargs["project_id"] == "d7011a46-5b44-404c-a2d8-f0024bb40920"
        assert "3c5416f9-ff13-4c16-9a07-71d0e5a8c090" not in str(sdk.init_kwargs)

    def test_prints_dashboard_url(self, sdk: FakeSDK, capsys: pytest.CaptureFixture[str]) -> None:
        get_observability().publish_score("llm_precision_at_k", 0.9)
        out = capsys.readouterr().out
        assert "llm_precision_at_k" in out
        assert braintrust_dashboard_url() in out

    def test_printed_url_never_contains_the_key(
        self, sdk: FakeSDK, capsys: pytest.CaptureFixture[str]
    ) -> None:
        get_observability().publish_score("m", 1.0)
        assert "sk-fake-not-real" not in capsys.readouterr().out

    def test_extra_scores_join_the_same_row(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0, extra_scores={"other": 0.5, "count": 0.0})
        assert sdk.logger.rows[0]["scores"] == {"m": 1.0, "other": 0.5, "count": 0.0}


class TestPublishFailureNeverPropagates:
    """A tracing fault must never fail the eval run that produced it."""

    def test_sdk_error_is_reported_not_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        install_sdk(FakeLogger(error=FakeSDKError("503 from braintrust")))
        with caplog.at_level(logging.WARNING):
            assert get_observability().publish_score("m", 1.0) is False
        assert [r.levelname for r in caplog.records] == ["WARNING"]
        assert "503 from braintrust" in caplog.records[0].getMessage()
        assert "m" in caplog.records[0].getMessage()

    def test_broken_integration_is_a_warning_not_an_exception(self) -> None:
        """A wrong SDK call must not take the run down with it."""
        install_sdk(returns=NoLogMethod())
        assert get_observability().publish_score("m", 1.0) is False

    def test_publish_failure_leaves_eval_result_untouched(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        install_sdk(FakeLogger(error=FakeSDKError("nope")))
        assert run_eval() is True
        out = capsys.readouterr().out
        assert out.startswith("Golden set cases:")
        assert "RESULT:                      PASS" in out


class TestEvalScoresPublished:
    def test_run_eval_publishes_year_accuracy(self, sdk: FakeSDK) -> None:
        assert run_eval() is True
        assert len(sdk.logger.rows) == 1
        row = sdk.logger.rows[0]
        assert row["scores"][YEAR_ACCURACY_SCORE] == row["output"]["score"]
        assert row["tags"] == [*EVAL_TAGS, YEAR_ACCURACY_SCORE]

    def test_run_eval_publishes_seniority_passthroughs(self, sdk: FakeSDK) -> None:
        report = evaluate(load_golden_set())
        run_eval()
        row = sdk.logger.rows[0]
        assert row["scores"][SENIORITY_PASSTHROUGH_SCORE] == float(
            len(report.experience_pass_through)
        )

    def test_run_eval_publishes_one_experiment_row(self, sdk: FakeSDK) -> None:
        run_eval()
        row = sdk.logger.rows[0]
        assert row["metadata"]["experiment"] == EVAL_EXPERIMENT
        assert set(row["scores"]) == {YEAR_ACCURACY_SCORE, SENIORITY_PASSTHROUGH_SCORE}

    def test_decision_agreement_is_not_published_without_labels(self, sdk: FakeSDK) -> None:
        """DEC6 is owner-gated: with no labelled ground truth the score is skipped.

        Inventing a ground truth here would manufacture the very number DEC6
        exists to produce, so the honest answer is a missing score.
        """
        run_eval()
        row = sdk.logger.rows[0]
        assert DECISION_AGREEMENT_SCORE not in row["scores"]
        assert not any("agreement" in name for name in row["scores"])
        assert not evaluation.DECISION_LABELS_PATH.exists()

    def test_run_eval_publishes_exact_report_numbers(self, sdk: FakeSDK) -> None:
        report = evaluate(load_golden_set())
        run_eval()
        row = sdk.logger.rows[0]
        assert row["scores"][YEAR_ACCURACY_SCORE] == report.year_accuracy
        assert row["metadata"]["year_correct"] == report.year_correct
        assert row["metadata"]["year_checked"] == report.year_checked
        assert row["metadata"]["accepted"] == report.accepted
        assert row["metadata"]["experience_pass_through"] == len(report.experience_pass_through)

    def test_evaluation_thresholds_are_untouched(self) -> None:
        """Publishing an experiment must not move a target or a definition."""
        assert evaluation.YEAR_ACCURACY_TARGET == 0.95
        assert evaluation.SKILL_ACCURACY_TARGET == 0.95
        assert EvalReport().accepted is True
        assert SENIORITY_PASSTHROUGH_SCORE == "seniority_passthroughs"
        assert DECISION_AGREEMENT_SCORE == "decision_agreement"

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


# --------------------------------------------------------------------------- #
# Realistic fixtures: a job and a profile carrying markers that must never ship
# --------------------------------------------------------------------------- #

PII_NAME = "Zaphod Testerson"
PII_EMAIL = "zaphod@example.com"
PII_PHONE = "+91 98765 43210"
PII_GITHUB = "https://github.com/zaphod"
PII_PORTFOLIO = "https://zaphod.example.me/cv"
PII_RESUME = "Built the billing service for ZARGO corp; 6 years Python and Django."
PII_JD_MARKER = "ZARGO billing platform"
PII_JOB_URL = "https://jobs.example.com/roles/4242"


def _pii_job() -> Job:
    return Job(
        source="t",
        url=PII_JOB_URL,
        dedupe_key="k",
        title="Junior Python Developer",
        company="Initech",
        location="Remote",
        remote_type="remote",
        description_raw=(
            f"Initech is hiring to work on the {PII_JD_MARKER}. "
            "0-2 years experience. Fully remote."
        ),
    )


def _pii_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name=PII_NAME,
        email=PII_EMAIL,
        phone=PII_PHONE,
        target_roles=["Backend"],
        total_experience_months=6,
        skills=[Skill(name="Python", canonical_name="python", proficiency=Proficiency.strong)],
    )


def _canned_client() -> LLMClient:
    """Fake client that walks the whole pipeline, and never touches the network."""
    return make_client(
        parsed={
            "title": "Junior Python Developer",
            "title_seniority": "junior",
            "min_years_experience": 0,
            "must_have_skills": ["Python"],
            "remote_type": "remote",
            "confidence": 0.9,
        },
        verdict={
            "fit_score": 85,
            "skills_fit": 90,
            "seniority_fit": 95,
            "role_fit": 80,
            "growth_fit": 75,
            "company_signal": 60,
            "recommendation": "apply",
            "reasons_for": [{"quote": f"work on the {PII_JD_MARKER}", "source": "jd"}],
            "reasons_against": [],
        },
        verifier={"veto": False, "downgrade_to": None, "reasons_against": []},
    )


#: Every string that must be absent from anything Atlas sends.
PII_MARKERS = (
    PII_NAME,
    PII_EMAIL,
    PII_PHONE,
    PII_GITHUB,
    PII_PORTFOLIO,
    PII_RESUME,
    PII_JD_MARKER,
)


# --------------------------------------------------------------------------- #
# The allowlist: the control that decides what may leave the process
# --------------------------------------------------------------------------- #


class TestAllowlistIsExplicit:
    def test_allowlist_is_a_non_empty_set_of_field_names(self) -> None:
        assert ALLOWED_PAYLOAD_KEYS
        for key in ALLOWED_PAYLOAD_KEYS:
            assert key.replace("_", "").isalnum(), key
            assert key == key.lower(), key

    def test_allowlist_excludes_pii_and_free_text_fields(self) -> None:
        """The list of what must never go out is spelled out, not implied."""
        forbidden = {
            "name",
            "full_name",
            "email",
            "phone",
            "resume",
            "resume_text",
            "profile",
            "profile_json",
            "description",
            "description_raw",
            "system",
            "user",
            "prompt",
            "prompt_text",
            "text",
            "args",
            "kwargs",
            "urls",
            "location",
            "company_signal",
            "reasons_for",
            "reasons_against",
            "notes",
        }
        assert forbidden.isdisjoint(ALLOWED_PAYLOAD_KEYS)

    def test_only_the_job_url_may_carry_a_link(self) -> None:
        assert ALLOWED_URL_FIELDS == {"job_url"}
        assert ALLOWED_URL_FIELDS.issubset(ALLOWED_PAYLOAD_KEYS)

    def test_dropping_is_a_filter_not_an_error(self) -> None:
        assert allowlist_payload({"job_id": 3, "resume_text": "secret"}) == {"job_id": 3}

    def test_non_mapping_values_pass_through(self) -> None:
        assert allowlist_payload(7) == 7
        assert allowlist_payload("apply") == "apply"
        assert allowlist_payload(None) is None
        assert allowlist_payload(["seniority_years"]) == ["seniority_years"]


class TestAllowlistDropsDisallowedKeys:
    def test_disallowed_metadata_key_is_dropped(self, sdk: FakeSDK) -> None:
        get_observability().publish_score(
            "m", 1.0, metadata={"stage": "eval", "resume_text": "SECRET"}
        )
        assert sdk.logger.rows[0]["metadata"] == {"stage": "eval"}

    def test_disallowed_input_key_is_dropped(self, sdk: FakeSDK) -> None:
        get_observability().publish_score(
            "m", 1.0, input={"eval": "golden_set", "description_raw": "JD text"}
        )
        assert sdk.logger.rows[0]["input"] == {"eval": "golden_set"}

    def test_span_metadata_drops_disallowed_keys(self, sdk: FakeSDK) -> None:
        span = get_observability().start_span(
            "pipeline.parse", metadata={"job_id": 7, "profile": "SECRET"}
        )
        span.end()
        assert sdk.spans[0].metadata()["job_id"] == 7
        assert "profile" not in sdk.spans[0].metadata()

    def test_span_set_metadata_drops_disallowed_keys(self, sdk: FakeSDK) -> None:
        span = get_observability().start_span("pipeline.filters")
        span.set_metadata(passed=True, resume_text="SECRET")
        span.end()
        meta = sdk.spans[0].metadata()
        assert meta["passed"] is True
        assert "resume_text" not in meta
        assert "SECRET" not in str(sdk.spans[0].sent())

    def test_nested_disallowed_keys_are_dropped(self, sdk: FakeSDK) -> None:
        """A parsed model dump as span output keeps numbers, loses the prose."""
        get_observability().start_span(
            "llm.call_json.Verdict",
            input={
                "verdict": {
                    "fit_score": 90,
                    "reasons_for": [{"quote": "SECRET"}],
                }
            },
        ).end()
        assert sdk.spans[0].start_kwargs["input"] == {}

    def test_nested_allowlisted_keys_are_still_filtered(self, sdk: FakeSDK) -> None:
        """Recursion reaches inside an allowed key, so the boundary is total."""
        get_observability().start_span(
            "pipeline.decision", input={"model_names": {"evaluator": "gpt", "email": "SECRET"}}
        ).end()
        assert sdk.spans[0].start_kwargs["input"] == {"model_names": {}}

    def test_dropped_fields_are_reported_at_warning_level(
        self, sdk: FakeSDK, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            get_observability().publish_score("m", 1.0, metadata={"resume_text": "SECRET"})
        dropped = [r for r in caplog.records if "non-allowlisted" in r.getMessage()]
        assert [r.levelname for r in dropped] == ["WARNING"]
        assert "resume_text" in dropped[0].getMessage()

    def test_unsafe_score_name_is_dropped(self, sdk: FakeSDK) -> None:
        get_observability().publish_score(
            "a score name with spaces", 1.0, extra_scores={"ok_score": 0.5}
        )
        row = sdk.logger.rows[0]
        assert row["scores"] == {"ok_score": 0.5}
        assert row["tags"] == list(EVAL_TAGS)

    def test_non_numeric_score_is_dropped(self, sdk: FakeSDK) -> None:
        get_observability().publish_score("m", 1.0, extra_scores={"word_score": "high"})
        assert sdk.logger.rows[0]["scores"] == {"m": 1.0}


class TestNoPiiReachesBraintrust:
    """The end-to-end proof: a real job + a real profile, serialized and searched."""

    def test_real_run_spans_carry_no_resume_profile_or_contact_detail(self, sdk: FakeSDK) -> None:
        result = process_job(_canned_client(), None, _pii_job(), _pii_profile(), get_settings())

        assert result.verdict is not None, "the run under test must reach the evaluator"
        assert sdk.spans, "the run under test must produce spans"
        blob = "".join(span.sent() for span in sdk.spans)
        for marker in PII_MARKERS:
            assert marker not in blob, marker

    def test_real_run_spans_still_carry_the_allowed_job_fields(self, sdk: FakeSDK) -> None:
        job = _pii_job()
        process_job(_canned_client(), None, job, _pii_profile(), get_settings())

        blob = "".join(span.sent() for span in sdk.spans)
        assert job.url in blob, "the job url is allowlisted and must survive"
        assert job.title in blob
        assert job.company in blob

    def test_careless_publish_payload_is_stripped_of_everything_private(self, sdk: FakeSDK) -> None:
        """Even a call site that tries to send PII cannot get it out."""
        job = _pii_job()
        overfull = {
            **_job_fields(7, job),
            "resume_text": PII_RESUME,
            "profile_json": f'{{"full_name": "{PII_NAME}", "email": "{PII_EMAIL}"}}',
            "name": PII_NAME,
            "email": PII_EMAIL,
            "phone": PII_PHONE,
            "links": [PII_GITHUB, PII_PORTFOLIO],
            "description_raw": PII_JD_MARKER,
        }
        get_observability().publish_score("m", 1.0, input=overfull, metadata=overfull)

        row = sdk.logger.rows[0]
        blob = str(row)
        for marker in PII_MARKERS:
            assert marker not in blob, marker
        assert row["input"] == {
            "job_id": 7,
            "job_title": job.title,
            "job_company": job.company,
            "job_url": job.url,
        }

    def test_allowed_url_field_is_not_url_redacted(self, sdk: FakeSDK) -> None:
        job = _pii_job()
        get_observability().publish_score("m", 1.0, metadata=_job_fields(None, job))
        assert sdk.logger.rows[0]["metadata"]["job_url"] == job.url

    def test_disallowed_link_is_still_redacted_by_defence_in_depth(self, sdk: FakeSDK) -> None:
        """A private link under an allowlisted key is masked even if the key stays."""
        get_observability().publish_score("m", 1.0, metadata={"job_title": PII_PORTFOLIO})
        assert PII_PORTFOLIO not in str(sdk.logger.rows[0]["metadata"])
        assert "<URL>_" in sdk.logger.rows[0]["metadata"]["job_title"]


# --------------------------------------------------------------------------- #
# LLM spans: no prompt text, token counts present, usage optional
# --------------------------------------------------------------------------- #


class _Item(BaseModel):
    value: int


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class _UsageResponse:
    """A Groq-shaped response, with or without a usage object."""

    def __init__(self, content: str, usage: Any) -> None:
        message = types.SimpleNamespace(content=content)
        self.choices = [types.SimpleNamespace(message=message)]
        if usage is not None:
            self.usage = usage


def _client_with_usage(prompt_tokens: int = 11, completion_tokens: int = 5) -> LLMClient:
    return LLMClient(
        client=types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(
                    create=lambda **_: _UsageResponse(
                        '{"value": 7}', _Usage(prompt_tokens, completion_tokens)
                    )
                )
            )
        )
    )


class TestLlmSpanCarriesNoPromptText:
    SYSTEM = "You are an adversarial reviewer for SLARTIBARTFAST."
    USER = "JD fragment: MARVIN the martian wants 0-2 years of Python."

    def test_prompt_text_is_never_sent(self, sdk: FakeSDK) -> None:
        client = _client_with_usage()
        client.call_json(schema=_Item, system=self.SYSTEM, user=self.USER)

        blob = sdk.spans[-1].sent()
        assert "SLARTIBARTFAST" not in blob
        assert "MARVIN" not in blob
        assert "adversarial reviewer" not in blob

    def test_span_input_carries_no_prompt(self, sdk: FakeSDK) -> None:
        client = _client_with_usage()
        client.call_json(schema=_Item, system=self.SYSTEM, user=self.USER)
        assert sdk.spans[-1].start_kwargs["input"] is None

    def test_span_records_prompt_length_and_hash(self, sdk: FakeSDK) -> None:
        client = _client_with_usage()
        client.call_json(schema=_Item, system=self.SYSTEM, user=self.USER)

        meta = sdk.spans[-1].metadata()
        assert meta["prompt_system_chars"] == len(self.SYSTEM)
        assert meta["prompt_user_chars"] == len(self.USER)
        expected = prompt_fingerprint(self.SYSTEM, "prompt_system")["prompt_system_sha"]
        assert meta["prompt_system_sha"] == expected
        assert len(meta["prompt_system_sha"]) == 12

    def test_same_prompt_hashes_the_same_and_different_ones_differ(self) -> None:
        a = prompt_fingerprint("hello", "prompt_system")["prompt_system_sha"]
        b = prompt_fingerprint("hello", "prompt_system")["prompt_system_sha"]
        c = prompt_fingerprint("hello there", "prompt_system")["prompt_system_sha"]
        assert a == b
        assert a != c

    def test_fingerprint_helper_refuses_an_unusable_field_name(self) -> None:
        assert prompt_fingerprint("x", "not a field name") == {}

    def test_model_and_schema_are_traced(self, sdk: FakeSDK) -> None:
        client = _client_with_usage()
        client.call_json(schema=_Item, system=self.SYSTEM, user=self.USER)
        meta = sdk.spans[-1].metadata()
        assert meta["model"] == client.default_model
        assert meta["schema"] == "_Item"

    def test_validation_failure_records_its_type_not_its_text(self, sdk: FakeSDK) -> None:
        """A validation message can quote the model's reply, so it is not sent."""
        client = LLMClient(
            client=types.SimpleNamespace(
                chat=types.SimpleNamespace(
                    completions=types.SimpleNamespace(
                        create=lambda **_: _UsageResponse("not json at all", _Usage(3, 1))
                    )
                )
            ),
            max_validation_retries=2,
        )
        with pytest.raises(LLMError):
            client.call_json(schema=_Item, system=self.SYSTEM, user=self.USER)
        meta = sdk.spans[-1].metadata()
        assert meta["validation_ok"] is False
        assert meta["validation_error_type"] == "JSONDecodeError"
        assert "Expecting value" not in str(meta)


class TestTokenCounts:
    def test_token_counts_are_recorded_on_the_span(self, sdk: FakeSDK) -> None:
        client = _client_with_usage(prompt_tokens=11, completion_tokens=5)
        client.call_json(schema=_Item, system="s", user="u")
        meta = sdk.spans[-1].metadata()
        assert meta["prompt_tokens"] == 11
        assert meta["completion_tokens"] == 5
        assert meta["total_tokens"] == 16

    def test_token_counts_survive_a_validation_retry(self, sdk: FakeSDK) -> None:
        """Every attempt's tokens are counted, so the span shows the real cost."""
        calls: list[int] = []

        def create(**_: Any):
            calls.append(1)
            return _UsageResponse("nope", _Usage(10 * len(calls), 2))

        client = LLMClient(
            client=types.SimpleNamespace(
                chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create))
            ),
            max_validation_retries=2,
        )
        with pytest.raises(LLMError):
            client.call_json(schema=_Item, system="s", user="u")
        meta = sdk.spans[-1].metadata()
        assert meta["prompt_tokens"] == 30
        assert meta["completion_tokens"] == 4

    def test_response_without_usage_is_tolerated(self, sdk: FakeSDK) -> None:
        """fake_llm's responses carry no usage; the call must still succeed."""
        client = make_client(verdict={"value": 7})
        assert client.call_json(schema=_Item, system="s", user="u").value == 7
        meta = sdk.spans[-1].metadata()
        assert "prompt_tokens" not in meta
        assert "total_tokens" not in meta

    def test_token_counts_never_reach_the_api_when_telemetry_is_off(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = _client_with_usage()
        assert get_observability().enabled is False
        assert client.call_json(schema=_Item, system="s", user="u").value == 7
        assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# Pipeline stage spans: observational only
# --------------------------------------------------------------------------- #


def _outcome(client: LLMClient) -> tuple:
    """Everything a run produces, so two runs can be compared for equality."""
    result = process_job(client, None, _pii_job(), _pii_profile(), get_settings())
    return (
        result.status,
        result.final_recommendation,
        result.score,
        result.needs_review,
        result.evidence_failed,
        result.parsed,
        result.filter_result,
        result.skill_match,
        result.verdict,
        result.verifier,
        result.decision,
    )


#: Every pipeline stage that must get exactly one span per job.
STAGE_NAMES = (
    "pipeline.parse",
    "pipeline.filters",
    "pipeline.skill_match",
    "pipeline.evaluator",
    "pipeline.verifier",
    "pipeline.decision",
)


class TestStageSpansAreObservational:
    def test_one_span_per_stage(self, sdk: FakeSDK) -> None:
        process_job(_canned_client(), None, _pii_job(), _pii_profile(), get_settings())
        names = [span.name for span in sdk.spans]
        for stage in STAGE_NAMES:
            assert names.count(stage) == 1, names

    def test_stage_spans_record_their_latency(self, sdk: FakeSDK) -> None:
        process_job(_canned_client(), None, _pii_job(), _pii_profile(), get_settings())
        for span in sdk.spans:
            assert span.ended
            assert isinstance(span.metadata()["latency_s"], float)

    def test_stage_spans_change_nothing_about_the_run(self, sdk: FakeSDK) -> None:
        traced_outcome = _outcome(_canned_client())
        assert get_observability().enabled is True

        reset_observability()
        assert get_observability().enabled is False
        quiet_outcome = _outcome(_canned_client())

        assert traced_outcome == quiet_outcome

    def test_a_span_that_cannot_start_leaves_the_run_intact(self) -> None:
        expected = _outcome(_canned_client())
        reset_observability()
        install_sdk(span_error=FakeSDKError("no spans for you"))
        assert _outcome(_canned_client()) == expected

    def test_a_span_that_cannot_end_leaves_the_run_intact(self) -> None:
        expected = _outcome(_canned_client())
        reset_observability()
        fake = install_sdk(span_end_error=FakeSDKError("cannot end"))
        assert _outcome(_canned_client()) == expected
        assert fake.spans, "spans were attempted"

    def test_stage_span_failures_are_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        reset_observability()
        install_sdk(span_error=FakeSDKError("nope"))
        with caplog.at_level(logging.WARNING):
            _outcome(_canned_client())
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings
        assert any("could not start" in r.getMessage() for r in warnings)

    def test_no_stage_span_without_a_key(self) -> None:
        """With no key configured the pipeline opens nothing and prints nothing."""
        reset_observability()
        with traced_stage("pipeline.parse", stage="parse") as span:
            assert span._handle is None
        assert span.metadata["latency_s"] >= 0


class TestRunSpan:
    def _seeded(self, tmp_path: Path, name: str = "obs.db"):
        conn = connect(tmp_path / name)
        init_db(conn)
        save_profile(conn, _pii_profile())
        run_id = start_run(conn, "run")
        upsert_job(conn, run_id, _pii_job())
        return conn, run_id

    def test_one_trace_per_run_wraps_every_stage(self, tmp_path: Path) -> None:
        conn, run_id = self._seeded(tmp_path)
        sdk = install_sdk()
        results = run_pipeline(conn, run_id, _pii_profile(), get_settings(), _canned_client())
        assert len(results) == 1
        names = [span.name for span in sdk.spans]
        assert names[0] == "pipeline.run"
        assert "pipeline.parse" in names
        assert "pipeline.decision" in names

    def test_run_span_counts_the_funnel(self, tmp_path: Path) -> None:
        conn, run_id = self._seeded(tmp_path)
        sdk = install_sdk()
        run_pipeline(conn, run_id, _pii_profile(), get_settings(), _canned_client())
        meta = sdk.spans[0].metadata()
        assert meta["processed"] == 1
        assert meta["filtered_out"] == 0
        assert meta["evaluated"] == 1
        assert meta["jobs"] == 1
        assert meta["run_id"] == run_id

    def test_run_pipeline_returns_the_same_results_without_tracing(self, tmp_path: Path) -> None:
        """Same input, two databases: the status machine cannot be replayed."""
        reset_observability()
        quiet_conn, quiet_run = self._seeded(tmp_path, "quiet.db")
        quiet = run_pipeline(
            quiet_conn, quiet_run, _pii_profile(), get_settings(), _canned_client()
        )
        install_sdk()
        traced_conn, traced_run = self._seeded(tmp_path, "traced.db")
        traced_out = run_pipeline(
            traced_conn, traced_run, _pii_profile(), get_settings(), _canned_client()
        )
        assert len(quiet) == len(traced_out) == 1
        assert [r.status for r in quiet] == [r.status for r in traced_out]
        assert [r.final_recommendation for r in quiet] == [
            r.final_recommendation for r in traced_out
        ]
        assert [r.score for r in quiet] == [r.score for r in traced_out]


class TestSdkCallShape:
    """Pins the real SDK's signatures so a mismatched call cannot ship again."""

    def test_result_is_logged_and_end_takes_no_event_fields(self, sdk: FakeSDK) -> None:
        """``Span.end(end_time=None)`` only: the event goes out through ``log()``."""
        span = get_observability().start_span("pipeline.filters", metadata={"job_id": 3})
        span.end(output={"score": 1})
        recorded = sdk.spans[0]
        assert recorded.events[-1]["output"] == {"score": 1}
        assert recorded.end_kwargs == {"end_time": None}

    def test_login_creates_a_logger_so_spans_have_a_parent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a logger for the project, ``start_span`` returns a no-op span."""
        fake = FakeSDK()
        monkeypatch.setitem(sys.modules, "braintrust", fake)
        monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-fake-not-real")
        monkeypatch.setattr(observability, "_in_offline_mode", lambda: False)
        obs = Observability()
        assert obs.init() is True
        assert fake.init_kwargs["project_id"] == DEFAULT_BRAINTRUST_PROJECT_ID

    def test_unusable_logger_leaves_tracing_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No logger means no real spans, so tracing stays off rather than silent."""
        fake = FakeSDK()
        monkeypatch.setitem(sys.modules, "braintrust", fake)
        monkeypatch.setenv("BRAINTRUST_API_KEY", "sk-fake-not-real")
        monkeypatch.setattr(observability, "_in_offline_mode", lambda: False)
        monkeypatch.setattr(fake, "init_logger", _raise_logger_error)
        obs = Observability()
        assert obs.init() is False
        assert obs.enabled is False


class TestTracedStageHelper:
    def test_metadata_reaches_the_span(self, sdk: FakeSDK) -> None:
        with traced_stage("stage", stage="parse", job_id=4) as span:
            span.set_metadata(score=1.5)
        assert sdk.spans[0].metadata() == {
            "stage": "parse",
            "job_id": 4,
            "score": 1.5,
            "latency_s": sdk.spans[0].metadata()["latency_s"],
        }

    def test_body_exception_propagates_unchanged(self, sdk: FakeSDK) -> None:
        with pytest.raises(ValueError, match="stage failed"), traced_stage("stage"):
            raise ValueError("stage failed")
        assert sdk.spans[0].ended

    def test_span_is_ended_even_when_the_body_raises(self, sdk: FakeSDK) -> None:
        with pytest.raises(ValueError), traced_stage("stage"):
            raise ValueError("boom")
        assert sdk.spans[0].ended

    def test_helper_is_inert_when_telemetry_is_off(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with traced_stage("stage", stage="parse") as span:
            span.set_metadata(score=2.0)
        assert span.metadata["latency_s"] >= 0
        assert capsys.readouterr().out == ""

    def test_traced_decorator_sends_no_arguments_or_result(self, sdk: FakeSDK) -> None:
        @traced("secret.fn")
        def takes_a_profile(profile: CandidateProfile) -> str:
            return profile.full_name

        assert takes_a_profile(_pii_profile()) == PII_NAME
        blob = sdk.spans[0].sent()
        assert PII_NAME not in blob
        assert PII_EMAIL not in blob
        assert sdk.spans[0].metadata()["arg_count"] == 1
        assert sdk.spans[0].metadata()["result_type"] == "str"
