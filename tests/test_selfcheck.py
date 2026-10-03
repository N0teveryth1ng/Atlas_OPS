"""Tests for the pre-Phase-8 self-check runner.

The audit (section 1) requires that the runner exits non-zero when any check
fails, emits a machine-readable report with environment metadata, and can be
made deterministic without network/email. These tests induce success and failure
without touching the real toolchain.
"""

from __future__ import annotations

import json

from atlas.selfcheck import FAIL, PASS, CheckResult, main, run_checks


def _passing(_ctx) -> CheckResult:
    return CheckResult("X", "always passes", PASS, "ok")


def _failing(_ctx) -> CheckResult:
    return CheckResult("Y", "always fails", FAIL, "boom")


def test_exit_nonzero_on_induced_failure():
    report = run_checks("fast", checks=[_failing])
    assert report.exit_code == 1


def test_exit_zero_when_all_pass():
    report = run_checks("fast", checks=[_passing])
    assert report.exit_code == 0


def test_report_contains_environment_metadata():
    data = run_checks("fast", checks=[_passing]).to_dict()
    for key in (
        "generated_at",
        "mode",
        "git_sha",
        "config_hash",
        "prompt_versions",
        "models",
        "checks",
        "summary",
        "exit_code",
    ):
        assert key in data
    assert data["mode"] == "fast"


def test_crashing_check_is_reported_as_fail():
    def _boom(_ctx):
        raise RuntimeError("kaboom")

    report = run_checks("fast", checks=[_boom])
    assert report.exit_code == 1
    assert report.checks[0].status == FAIL
    assert "kaboom" in report.checks[0].evidence


def test_main_writes_report(tmp_path):
    out = tmp_path / "selfcheck_report.json"
    code = main(["--fast", "--report", str(out)], print_fn=lambda *a, **k: None)
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["checks"], "fast run must emit at least one check"
    assert isinstance(code, int)


def test_cli_registers_selfcheck():
    from atlas.cli import build_parser

    args = build_parser().parse_args(["selfcheck", "--fast"])
    assert args.func.__name__ == "cmd_selfcheck"
    assert args.full is False
