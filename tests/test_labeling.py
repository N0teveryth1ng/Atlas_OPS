"""Tests for the read-only ``atlas label-check`` validator.

The validator must report counts/minimums and must never create or edit labels.
"""

from __future__ import annotations

import json
from pathlib import Path

from atlas import labeling
from atlas.cli import main


def _write(tmp_path: Path, entries: list[dict | str]) -> Path:
    path = tmp_path / "labels.jsonl"
    lines = [e if isinstance(e, str) else json.dumps(e) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _entry(pool_id: str, label: str, reason: str = "ok", **extra) -> dict:
    entry = {
        "pool_id": pool_id,
        "url": f"https://example.test/{pool_id}",
        "label": label,
        "reason": reason,
        "labeled_by": "human",
    }
    entry.update(extra)
    return entry


def _complete_pool() -> list[dict]:
    entries = [_entry(f"apply-{i}", "apply", "strong match on python") for i in range(10)]
    entries += [_entry(f"trap-{i}", "skip", "senior role requiring 8 years") for i in range(15)]
    entries += [_entry(f"skip-{i}", "skip", "unrelated stack") for i in range(15)]
    return entries


def test_complete_pool_meets_minimums(tmp_path: Path) -> None:
    report = labeling.validate_labels(_write(tmp_path, _complete_pool()))
    assert report.valid == 40
    assert report.applies == 10
    assert report.skips == 30
    assert report.traps == 15
    assert report.issues == []
    assert report.meets_minimums
    assert report.shortfalls == []


def test_missing_file_reports_issue(tmp_path: Path) -> None:
    report = labeling.validate_labels(tmp_path / "nope.jsonl")
    assert report.valid == 0
    assert not report.meets_minimums
    assert len(report.issues) == 1
    assert "not found" in report.issues[0].message


def test_invalid_json_line_is_reported(tmp_path: Path) -> None:
    report = labeling.validate_labels(_write(tmp_path, ["{not json"]))
    assert report.valid == 0
    assert "invalid JSON" in report.issues[0].message


def test_non_object_entry_is_reported(tmp_path: Path) -> None:
    report = labeling.validate_labels(_write(tmp_path, ["[1, 2, 3]"]))
    assert "not a JSON object" in report.issues[0].message


def test_missing_required_fields_are_reported(tmp_path: Path) -> None:
    bad = _entry("p1", "apply")
    del bad["url"]
    report = labeling.validate_labels(_write(tmp_path, [bad]))
    assert report.valid == 0
    assert "url" in report.issues[0].message


def test_invalid_label_is_reported(tmp_path: Path) -> None:
    report = labeling.validate_labels(_write(tmp_path, [_entry("p1", "maybe")]))
    assert "invalid label" in report.issues[0].message


def test_labeled_by_must_be_human(tmp_path: Path) -> None:
    report = labeling.validate_labels(_write(tmp_path, [_entry("p1", "apply", labeled_by="bot")]))
    assert report.valid == 0
    assert "labeled_by" in report.issues[0].message


def test_trap_detection_variants(tmp_path: Path) -> None:
    entries = [
        _entry("a", "skip", "requires 10 years"),  # reason keyword
        _entry("b", "skip", "does not say much", trap_type="seniority"),  # explicit type
        _entry("c", "skip", "does not say much", trap=True),  # explicit flag
        _entry("d", "skip", "wrong stack entirely"),  # not a trap
        _entry("e", "apply", "senior title but fits"),  # apply never a trap
        _entry("f", "skip", "experience level overqualifies"),  # keyword
    ]
    report = labeling.validate_labels(_write(tmp_path, entries))
    assert report.valid == 6
    assert report.traps == 4
    assert report.applies == 1


def test_blank_and_comment_lines_are_ignored(tmp_path: Path) -> None:
    entries = ["", "# a comment", *[json.dumps(_entry("p1", "apply"))]]
    report = labeling.validate_labels(_write(tmp_path, entries))
    assert report.valid == 1
    assert report.issues == []


def test_shortfalls_reported_when_incomplete(tmp_path: Path) -> None:
    report = labeling.validate_labels(_write(tmp_path, [_entry("p1", "apply")]))
    assert not report.meets_minimums
    text = " ".join(report.shortfalls)
    assert "40 valid" in text
    assert "15 traps" in text
    assert "10 apply" in text


def test_format_report_ok_and_incomplete(tmp_path: Path) -> None:
    ok = labeling.format_report(labeling.validate_labels(_write(tmp_path, _complete_pool())))
    assert "OK - all minimums met" in ok
    assert "valid cases : 40" in ok

    short = labeling.format_report(
        labeling.validate_labels(_write(tmp_path, [_entry("p1", "skip", "unrelated")]))
    )
    assert "INCOMPLETE" in short


def test_cli_label_check_exit_codes(tmp_path: Path) -> None:
    good = _write(tmp_path, _complete_pool())
    assert main(["label-check", "--labels", str(good)]) == 0

    short = tmp_path / "short.jsonl"
    short.write_text(json.dumps(_entry("p1", "apply")) + "\n", encoding="utf-8")
    assert main(["label-check", "--labels", str(short)]) == 1


def test_unicode_line_separator_does_not_split_record(tmp_path: Path) -> None:
    # U+0085 (NEL) can appear inside real JD text; str.splitlines() would wrongly
    # break the JSON record there.
    path = tmp_path / "labels.jsonl"
    path.write_text(
        '{"pool_id": "p1", "url": "u", "label": "apply", '
        '"reason": "fits\u0085well", "labeled_by": "human"}\n',
        encoding="utf-8",
    )
    report = labeling.validate_labels(path)
    assert report.valid == 1
    assert report.issues == []


def test_cli_never_writes_labels(tmp_path: Path) -> None:
    path = _write(tmp_path, _complete_pool())
    before = path.read_bytes()
    main(["label-check", "--labels", str(path)])
    assert path.read_bytes() == before
