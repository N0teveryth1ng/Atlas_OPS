"""Tests for the companies.yaml template."""

from __future__ import annotations

from atlas.companies import COMPANIES_PATH, load_companies

ATS_SOURCES = (
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "recruitee",
    "teamtailor",
)


def test_committed_template_declares_every_ats_source():
    companies = load_companies(COMPANIES_PATH)
    assert set(companies) == set(ATS_SOURCES)


def test_committed_template_has_no_tokens():
    """Empty lists keep every ATS collector skipped, as documented in the file."""
    companies = load_companies(COMPANIES_PATH)
    assert all(companies[source] == [] for source in ATS_SOURCES)


def test_missing_file_yields_empty_mapping(tmp_path):
    assert load_companies(tmp_path / "absent.yaml") == {}
