"""Read-only validation for a human-labelled golden-set file.

``atlas label-check`` uses this to sanity-check the labels a human provides
before they are folded into ``eval/golden_set.jsonl``. It only ever **reads**
and reports - it never creates, edits or infers labels.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

VALID_LABELS = frozenset({"apply", "skip"})
REQUIRED_FIELDS = ("pool_id", "url", "label", "reason", "labeled_by")

MIN_CASES = 40
MIN_TRAPS = 15
MIN_APPLY = 10

# A skipped case counts as a seniority/experience "trap" when either an explicit
# trap_type says so, a trap flag is set, or the free-text reason mentions
# seniority, experience or level signals.
SENIORITY_RE = re.compile(
    r"\b(senior|seniority|lead|principal|staff|manager|director|"
    r"years?|yrs?|experience|overqualif|mid[- ]?level)\b",
    re.IGNORECASE,
)


@dataclass
class LabelIssue:
    line: int
    message: str


@dataclass
class LabelReport:
    path: str
    valid: int = 0
    applies: int = 0
    skips: int = 0
    traps: int = 0
    issues: list[LabelIssue] = field(default_factory=list)

    @property
    def meets_minimums(self) -> bool:
        return not self.issues and (
            self.valid >= MIN_CASES and self.traps >= MIN_TRAPS and self.applies >= MIN_APPLY
        )

    @property
    def shortfalls(self) -> list[str]:
        out: list[str] = []
        if self.valid < MIN_CASES:
            out.append(f"need {MIN_CASES} valid cases (have {self.valid})")
        if self.traps < MIN_TRAPS:
            out.append(f"need {MIN_TRAPS} traps (have {self.traps})")
        if self.applies < MIN_APPLY:
            out.append(f"need {MIN_APPLY} apply cases (have {self.applies})")
        return out


def _is_trap(entry: dict, label: str) -> bool:
    if label != "skip":
        return False
    if str(entry.get("trap_type", "")).lower() in {"seniority", "experience"}:
        return True
    if entry.get("trap") is True:
        return True
    return bool(SENIORITY_RE.search(str(entry.get("reason", ""))))


def validate_labels(path: Path | str) -> LabelReport:
    target = Path(path)
    report = LabelReport(path=str(target))
    if not target.exists():
        report.issues.append(LabelIssue(0, "labels file not found"))
        return report
    # Read line-by-line on newlines only: ``str.splitlines`` would also break on
    # Unicode separators (e.g. U+0085) that legitimately appear inside JSON
    # string values in real JDs.
    text = target.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            report.issues.append(LabelIssue(lineno, f"invalid JSON: {exc.msg}"))
            continue
        if not isinstance(entry, dict):
            report.issues.append(LabelIssue(lineno, "entry is not a JSON object"))
            continue
        missing = [name for name in REQUIRED_FIELDS if not str(entry.get(name, "")).strip()]
        if missing:
            report.issues.append(LabelIssue(lineno, f"missing/empty: {', '.join(missing)}"))
            continue
        label = str(entry["label"]).strip().lower()
        if label not in VALID_LABELS:
            report.issues.append(
                LabelIssue(lineno, f"invalid label {entry['label']!r} (use apply|skip)")
            )
            continue
        if str(entry["labeled_by"]).strip().lower() != "human":
            report.issues.append(LabelIssue(lineno, "labeled_by must be 'human'"))
            continue
        report.valid += 1
        if label == "apply":
            report.applies += 1
        else:
            report.skips += 1
            if _is_trap(entry, label):
                report.traps += 1
    return report


def format_report(report: LabelReport) -> str:
    lines = [
        f"label-check: {report.path}",
        f"  valid cases : {report.valid}",
        f"  apply       : {report.applies}",
        f"  skip        : {report.skips}",
        f"  traps       : {report.traps}  (skip cases flagged seniority/experience)",
        f"  problems    : {len(report.issues)}",
    ]
    for issue in report.issues:
        lines.append(f"    line {issue.line}: {issue.message}")
    lines.append(
        f"  minimums    : >= {MIN_CASES} valid, >= {MIN_TRAPS} traps, >= {MIN_APPLY} apply"
    )
    if report.meets_minimums:
        lines.append("  result      : OK - all minimums met")
    else:
        lines.append("  result      : INCOMPLETE")
        for short in report.shortfalls:
            lines.append(f"    - {short}")
        if report.issues:
            lines.append(f"    - {len(report.issues)} invalid line(s) to fix")
    return "\n".join(lines)
