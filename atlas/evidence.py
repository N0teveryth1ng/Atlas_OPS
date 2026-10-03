"""Evidence-quote validation (audit D-3).

Every evaluator/verifier evidence item must cite a verbatim quote from either
the raw job description (``jd``) or the rendered candidate profile
(``profile``). Matching is whitespace- and case-insensitive only — no fuzzy
matching, no stemming — and quotes shorter than ``MIN_QUOTE_LEN`` are rejected.
"""

from __future__ import annotations

from .schemas import Evidence, EvidenceSource, Verdict, VerifierVerdict

MIN_QUOTE_LEN = 8


class EvidenceValidationError(RuntimeError):
    """Raised when a verdict cites evidence not present in the source."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _is_valid(evidence: Evidence, jd_text: str, profile_text: str) -> bool:
    quote = _normalize(evidence.quote)
    if len(quote) < MIN_QUOTE_LEN:
        return False
    source = jd_text if evidence.source == EvidenceSource.jd else profile_text
    return quote in _normalize(source)


def _check_items(items: list[Evidence], label: str, jd_text: str, profile_text: str) -> list[str]:
    errors: list[str] = []
    for index, evidence in enumerate(items):
        if not _is_valid(evidence, jd_text, profile_text):
            errors.append(
                f"{label}[{index}] quote not found in {evidence.source.value}: "
                f"{evidence.quote!r}"
            )
    return errors


def validate_verdict_evidence(verdict: Verdict, jd_text: str, profile_text: str) -> list[str]:
    errors = _check_items(verdict.reasons_for, "reasons_for", jd_text, profile_text)
    errors += _check_items(verdict.reasons_against, "reasons_against", jd_text, profile_text)
    if verdict.seniority_assessment is not None and not _is_valid(
        verdict.seniority_assessment, jd_text, profile_text
    ):
        errors.append(
            "seniority_assessment quote not found in "
            f"{verdict.seniority_assessment.source.value}: "
            f"{verdict.seniority_assessment.quote!r}"
        )
    return errors


def validate_verifier_evidence(
    verifier: VerifierVerdict, jd_text: str, profile_text: str
) -> list[str]:
    return _check_items(verifier.reasons_against, "reasons_against", jd_text, profile_text)


def validation_retry_message(errors: list[str]) -> str:
    joined = "\n".join(f"- {error}" for error in errors)
    return (
        "Your previous answer was REJECTED: every quote in reasons_for, reasons_against, "
        "and seniority_assessment must be a verbatim substring of the cited source (the raw "
        "job posting for source=jd, the candidate profile for source=profile), at least "
        f"{MIN_QUOTE_LEN} characters, whitespace/case differences allowed. Offending items:\n"
        f"{joined}\n"
        "Return corrected JSON only."
    )
