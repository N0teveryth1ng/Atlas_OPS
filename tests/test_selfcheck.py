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


def test_pytest_signature_strips_timing():
    from atlas.selfcheck import _pytest_signature

    assert _pytest_signature("1 passed, 2 warnings in 3.14s") == "1 passed, 2 warnings"
    assert _pytest_signature("439 passed in 4.40s") == "439 passed"


def test_readme_check_passes_on_this_repo():
    from atlas.selfcheck import PASS, check_h7_readme

    result = check_h7_readme({})
    assert result.status == PASS, result.evidence


def test_apply_isolation_checks_pass_on_this_repo():
    from atlas.selfcheck import (
        PASS,
        check_a1_autoapply_isolation,
        check_a2_no_application_posts,
    )

    assert check_a1_autoapply_isolation({}).status == PASS
    assert check_a2_no_application_posts({}).status == PASS


def test_working_tree_check_flags_branch(monkeypatch):
    from atlas import selfcheck

    def fake_run(cmd, timeout=600):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "feature/x\n"
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return 0, ""
        return 0, ""

    monkeypatch.setattr(selfcheck, "_run", fake_run)
    result = selfcheck.check_h1_working_tree({})
    assert result.status == FAIL
    assert "not 'main'" in result.evidence
