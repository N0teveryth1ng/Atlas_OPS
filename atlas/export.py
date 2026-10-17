"""ATS-friendly export of an approved tailored resume to DOCX (python-docx) and PDF (fpdf2).

Both formats are a single column of real, selectable text built from the same
:func:`atlas.resume.resume_blocks` the ATS linter checks, so the exported file and the
linted content can never drift. There are no tables, images, text boxes, icons or
header/footer content, the headings are the standard five, and a standard font is used
(Calibri in DOCX, Helvetica in PDF).

Smart punctuation (non-breaking hyphen, curly quotes, en/em dashes, ellipsis) is folded
to ASCII first: it keeps the PDF on a standard core font and makes the extracted text of
both files identical. Nothing here submits an application or fetches a URL.
"""

from __future__ import annotations

import io
import re
import unicodedata
from typing import Any

from .resume import ResumeBlockKind, TailoredResume, resume_blocks

#: Characters folded to their ASCII equivalent for the exported text.
_ASCII_MAP: dict[str, str] = {
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2212": "-",  # minus sign
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u2026": "...",
    "\u00a0": " ",
    "\u2022": "-",
    "\u00b7": "-",
    "\u200b": "",
    "\ufeff": "",
}


def to_ascii(text: Any) -> str:
    """Fold smart punctuation and accents to plain ASCII for a standard-font export."""
    if text is None:
        return ""
    source = str(text)
    out: list[str] = []
    for char in source:
        if char in _ASCII_MAP:
            out.append(_ASCII_MAP[char])
        elif ord(char) < 128:
            out.append(char)
        else:
            decomposed = unicodedata.normalize("NFKD", char)
            out.append(decomposed.encode("ascii", "ignore").decode("ascii"))
    return "".join(out)


def header_lines(resume: TailoredResume) -> list[str]:
    """The name line and the contact line(s), taken verbatim from the resume header."""
    lines: list[str] = []
    if resume.header.name:
        lines.append(resume.header.name)
    contact: list[str] = []
    if resume.header.email:
        contact.append(str(resume.header.email))
    links = resume.header.links
    if isinstance(links, dict):
        contact.extend(str(value) for value in links.values() if value)
    elif links:
        contact.append(str(links))
    if contact:
        lines.append(" | ".join(contact))
    return lines


def export_lines(resume: TailoredResume) -> list[str]:
    """Every exported line, in order: header, then the same blocks the ATS linter sees.

    Text is folded to ASCII and carries no bullet marker, so the extracted text of the
    DOCX and the PDF is byte-for-byte the same line sequence.
    """
    lines = [to_ascii(line) for line in header_lines(resume)]
    lines.extend(to_ascii(block.text) for block in resume_blocks(resume))
    return lines


def export_text(resume: TailoredResume) -> str:
    """The full exported document as plain text (one line per block)."""
    return "\n".join(export_lines(resume))


def export_filename(resume: TailoredResume, job: Any) -> str:
    """``Name_Role_Company`` with no extension, safe for a download header."""
    parts = [
        _slug(resume.header.name or "Resume"),
        _slug(getattr(job, "title", "") or "Role"),
        _slug(getattr(job, "company", "") or "Company"),
    ]
    return "_".join(part for part in parts if part) or "resume"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", to_ascii(text))


def export_docx(resume: TailoredResume) -> bytes:
    """Render the resume to a single-column DOCX of real text (no tables or images)."""
    from docx import Document
    from docx.shared import Pt

    document = Document()
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for line in header_lines(resume):
        document.add_paragraph(to_ascii(line))
    for block in resume_blocks(resume):
        text = to_ascii(block.text)
        if block.kind == ResumeBlockKind.heading:
            document.add_heading(text, level=1)
        elif block.kind == ResumeBlockKind.bullet:
            document.add_paragraph(text, style="List Bullet")
        else:
            document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def export_pdf(resume: TailoredResume) -> bytes:
    """Render the resume to a single-column PDF of real, selectable text."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    for line in header_lines(resume):
        pdf.multi_cell(0, 6, to_ascii(line), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    for block in resume_blocks(resume):
        text = to_ascii(block.text)
        if block.kind == ResumeBlockKind.heading:
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(0, 8, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_font("Helvetica", "", 11)
        elif block.kind == ResumeBlockKind.bullet:
            pdf.multi_cell(0, 6, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        else:
            pdf.multi_cell(0, 6, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())
