"""PDF text extraction.

``pypdf`` returns ``None`` from ``extract_text`` for pages with no text layer
(scanned images). We handle that explicitly and fail with a clear message
instead of crashing on a ``"\n".join`` over ``None``.
"""

from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader


class PdfExtractionError(RuntimeError):
    """Raised when a PDF yields no extractable text."""


def extract_text(source: bytes | str | Path) -> str:
    """Extract text from raw PDF bytes or a file path."""
    if isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    else:
        data = source

    reader = PdfReader(io.BytesIO(data))
    pages: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(text)

    combined = "\n".join(pages).strip()
    if not combined:
        raise PdfExtractionError(
            "No extractable text found in the PDF. It may be scanned; OCR is not enabled yet."
        )
    return combined
