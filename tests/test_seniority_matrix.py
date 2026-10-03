"""Seniority + required-years matrix (audit sections F1-F4).

Loads a human-authored fixture of >=150 labelled titles / year requirements and
asserts both the deterministic parser and the hard filter. This is the
regression net for the highest-risk failure mode of the product: letting an
over-qualified role through, or falsely rejecting a fresher-friendly one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.config import get_settings
from atlas.filters import DISALLOWED_SENIORITY, apply_hard_filters
from atlas.jd_parser import extract_years, parse_jd_regex_only
from atlas.schemas import CandidateProfile, Job, Seniority

CASES_PATH = Path(__file__).with_name("seniority_matrix_cases.json")


def load_cases() -> list[dict]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    assert len(cases) >= 150, f"seniority matrix must have >=150 cases, has {len(cases)}"
    return cases


def test_matrix_has_enough_cases():
    assert len(load_cases()) >= 150


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c["id"])
def test_seniority_and_year_matrix(case: dict):
    settings = get_settings()
    profile = CandidateProfile(
        total_experience_months=int(settings.candidate.my_years_experience * 12)
    )
    description = case.get("description", "Build software with the team.")
    parsed = parse_jd_regex_only(case["title"], description)
    job = Job(
        source="matrix",
        title=case["title"],
        url=f"matrix://{case['id']}",
        description_raw=description,
    )
    result = apply_hard_filters(job, parsed, profile, settings)

    if case["kind"] == "title":
        expected_level = Seniority(case["expected_seniority"])
        assert parsed.title_seniority == expected_level
        expected_reject = expected_level in DISALLOWED_SENIORITY
    else:
        low, _, _ = extract_years(description)
        assert low == case["expected_min_years"]
        expected_reject = case["expected_reject"]
        assert parsed.title_seniority not in DISALLOWED_SENIORITY

    assert result.passed is (not expected_reject), (
        f"{case['id']}: expected reject={expected_reject} but filter "
        f"passed={result.passed} ({[r.rule_id for r in result.rejections]})"
    )
