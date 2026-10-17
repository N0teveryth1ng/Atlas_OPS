"""DOCX/PDF export tests: real selectable text, ATS-friendly, and text that matches.

The exported text of both files must equal :func:`atlas.export.export_text` (folded to
ASCII), so there is a single source of truth shared with the ATS linter. No network.
"""

from __future__ import annotations

import io

from docx import Document
from pypdf import PdfReader

from atlas.export import (
    export_docx,
    export_filename,
    export_lines,
    export_pdf,
    export_text,
    to_ascii,
)
from atlas.resume import (
    STANDARD_HEADINGS,
    ATSReport,
    ExperienceEntry,
    ProjectEntry,
    RequirementCoverage,
    ResumeHeader,
    ResumeLine,
    TailoredResume,
    ats_lint,
)
from atlas.schemas import Job


def _resume() -> TailoredResume:
    return TailoredResume(
        header=ResumeHeader(
            name="Ada Lovelace",
            email="ada@example.com",
            links={"github": "https://github.com/ada"},
        ),
        summary=[ResumeLine(text="Builds job pipelines\u2014fast.", facts=["W1.s1"])],
        skills=["Python", "FastAPI"],
        familiar_skills=["Rust"],
        additional_tools=["Nginx"],
        experience=[
            ExperienceEntry(
                role="Data engineer",
                company="Acme",
                duration_months=30,
                dates="Jan 2020 - Jan 2023",
                bullets=[ResumeLine(text="Builds a job pipeline in Python.", facts=["W1.s1"])],
            )
        ],
        projects=[
            ProjectEntry(
                title="Atlas Ops",
                tech=["Python"],
                bullets=[ResumeLine(text="Shipped a live job pipeline.", facts=["P1.outcomes.s1"])],
            )
        ],
        education=[
            {
                "degree": "BSc",
                "field": "Computer Science",
                "institution": "University of X",
                "graduation_year": 2020,
            }
        ],
        certifications=["AWS Certified"],
        requirements=[RequirementCoverage(requirement="Python", status="covered", facts=["W1.s1"])],
        ats=ATSReport(passed=True),
    )


def _job() -> Job:
    return Job(
        source="test",
        url="https://acme.example/jobs/1",
        dedupe_key="acme-1",
        title="Backend Engineer",
        company="Globex",
    )


def test_to_ascii_folds_smart_punctuation():
    assert to_ascii("a\u2011b\u2013c\u2014d \u2019x\u201d \u2026") == "a-b-c-d 'x\" ..."


def test_export_lines_include_header_and_standard_headings():
    lines = export_lines(_resume())
    assert lines[0] == "Ada Lovelace"
    for heading in STANDARD_HEADINGS:
        assert heading in lines


def test_export_lines_are_ascii_and_omit_additional_tools():
    lines = export_lines(_resume())
    assert all(line.isascii() for line in lines)
    assert not any("Nginx" in line for line in lines)


def test_export_filename_is_name_role_company():
    assert export_filename(_resume(), _job()) == "AdaLovelace_BackendEngineer_Globex"


def _normalize(text: str) -> str:
    return " ".join(text.split())


def test_docx_text_matches_the_exported_content_and_has_no_tables():
    data = export_docx(_resume())
    document = Document(io.BytesIO(data))
    extracted = "\n".join(paragraph.text for paragraph in document.paragraphs)

    assert _normalize(extracted) == _normalize(export_text(_resume()))
    assert document.tables == []


def test_pdf_text_matches_the_exported_content_and_is_selectable():
    data = export_pdf(_resume())
    reader = PdfReader(io.BytesIO(data))
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert _normalize(extracted) == _normalize(export_text(_resume()))


def test_exported_docx_and_pdf_text_agree():
    resume = _resume()
    docx_text = "\n".join(p.text for p in Document(io.BytesIO(export_docx(resume))).paragraphs)
    pdf_text = "\n".join(
        (page.extract_text() or "") for page in PdfReader(io.BytesIO(export_pdf(resume))).pages
    )
    assert _normalize(docx_text) == _normalize(pdf_text)


def test_exported_content_passes_the_ats_lint():
    resume = _resume()
    assert ats_lint(resume).passed is True
    text = export_text(resume)
    for heading in STANDARD_HEADINGS:
        assert heading in text
