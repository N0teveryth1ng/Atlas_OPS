import pytest

from atlas.jd_parser import (
    detect_remote_type,
    detect_title_seniority,
    extract_years,
    merge_jd,
    parse_jd_regex_only,
)
from atlas.schemas import ParsedJD, RemoteType, Seniority


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3+ years", (3.0, None)),
        ("2-4 years of experience", (2.0, 4.0)),
        ("minimum of 5 years", (5.0, None)),
        ("at least 2 years", (2.0, None)),
        ("5 years of experience", (5.0, None)),
        ("0-1 years", (0.0, 1.0)),
        ("no experience required", (0.0, None)),
        ("fresher", (0.0, None)),
    ],
)
def test_extract_years(text, expected):
    low, high, _ = extract_years(text)
    assert (low, high) == expected


def test_extract_years_none_when_absent():
    assert extract_years("Build great things") == (None, None, None)


def test_extract_years_conservative_picks_highest_min():
    low, _, _ = extract_years("Ideally 2+ years, but really 5 years of experience needed")
    assert low == 5.0


@pytest.mark.parametrize(
    "title,level",
    [
        ("Software Engineer", Seniority.unknown),
        ("Junior Backend Developer", Seniority.junior),
        ("Senior Software Engineer", Seniority.senior),
        ("Staff Engineer", Seniority.principal),
        ("Engineering Manager", Seniority.manager),
        ("Lead Data Scientist", Seniority.lead),
        ("Principal Architect", Seniority.architect),
        ("Python Developer Intern", Seniority.intern),
        ("New Graduate Software Engineer", Seniority.entry),
    ],
)
def test_detect_title_seniority(title, level):
    assert detect_title_seniority(title) == level


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Fully remote role", RemoteType.remote),
        ("Hybrid, 3 days a week", RemoteType.hybrid),
        ("On-site in Bengaluru", RemoteType.onsite),
        ("Join our team", RemoteType.unknown),
    ],
)
def test_detect_remote_type(text, expected):
    assert detect_remote_type(text) == expected


def test_merge_takes_more_conservative_years_and_flags():
    regex = ParsedJD(min_years_experience=2)
    llm = ParsedJD(min_years_experience=7, confidence=1.0)
    merged = merge_jd(regex, llm)
    assert merged.min_years_experience == 7
    assert "years_disagree" in merged.ambiguities
    assert merged.confidence <= 0.5


def test_merge_more_senior_title_wins():
    regex = ParsedJD(title_seniority=Seniority.senior)
    llm = ParsedJD(title_seniority=Seniority.junior)
    assert merge_jd(regex, llm).title_seniority == Seniority.senior


def test_regex_only_parse_reports_confidence():
    parsed = parse_jd_regex_only("Junior Python Developer", "1+ years of experience with Python.")
    assert parsed.min_years_experience == 1
    assert parsed.confidence >= 0.9
