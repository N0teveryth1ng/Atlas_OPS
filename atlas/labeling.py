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


def _url_matches(pool_entry: dict, label_url: str) -> bool:
    pool_url = str(pool_entry.get("url", "")).strip()
    if pool_url and pool_url == label_url:
        return True
    urls = pool_entry.get("urls")
    if isinstance(urls, list):
        for u in urls:
            try:
                if str(u).strip() == label_url:
                    return True
            except (TypeError, AttributeError):
                continue
    return False


def validate_labels(
    path: Path | str,
    pool_path: Path | str = Path("eval/labeling_pool.jsonl"),
    pool_by_id: dict[str, dict] | None = None,
) -> LabelReport:
    target = Path(path)
    report = LabelReport(path=str(target))
    pool_p = Path(pool_path)
    if not target.exists():
        report.issues.append(LabelIssue(0, "labels file not found"))
        return report
    pool_by_id_map: dict[str, dict] = {}
    if pool_by_id is not None:
        pool_by_id_map.update(dict(pool_by_id))
    else:
        if not pool_p.exists():
            report.issues.append(LabelIssue(0, f"pool file not found: {pool_p}"))
            return report
        text_pool = pool_p.read_text(encoding="utf-8")
        for raw in text_pool.split("\n"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                pid = str(entry.get("pool_id", "")).strip()
                if pid:
                    pool_by_id_map.setdefault(pid, entry)
    text = target.read_text(encoding="utf-8")
    seen_pool_ids: dict[str, int] = {}
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
        pid = str(entry["pool_id"]).strip()
        if pid not in pool_by_id_map:
            report.issues.append(LabelIssue(lineno, "unknown pool_id"))
            continue
        if pid in seen_pool_ids:
            first = seen_pool_ids[pid]
            report.issues.append(LabelIssue(lineno, f"duplicate pool_id (first at line {first})"))
            continue
        label_url = str(entry["url"]).strip()
        if not _url_matches(pool_by_id_map[pid], label_url):
            report.issues.append(LabelIssue(lineno, "url does not match pool entry"))
            continue
        seen_pool_ids[pid] = lineno
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
