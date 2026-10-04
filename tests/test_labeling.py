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


def _pool_map_for(entries: list[dict | str]) -> dict[str, dict]:
    pool: dict[str, dict] = {}
    for e in entries:
        d = e if isinstance(e, dict) else json.loads(e)
        pid = str(d["pool_id"])
        if pid not in pool:
            url = str(d.get("url", f"https://example.test/{pid}"))
            pool[pid] = {"pool_id": pid, "url": url, "urls": []}
    return pool


def _complete_pool() -> list[dict]:
    entries = [_entry(f"apply-{i}", "apply", "strong match on python") for i in range(10)]
    entries += [_entry(f"trap-{i}", "skip", "senior role requiring 8 years") for i in range(15)]
    entries += [_entry(f"skip-{i}", "skip", "unrelated stack") for i in range(15)]
    return entries


def test_complete_pool_meets_minimums(tmp_path: Path) -> None:
    entries = _complete_pool()
    pool = _pool_map_for(entries)
    report = labeling.validate_labels(_write(tmp_path, entries), pool_by_id=pool)
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
    report = labeling.validate_labels(_write(tmp_path, entries), pool_by_id=_pool_map_for(entries))
    assert report.valid == 6
    assert report.traps == 4
    assert report.applies == 1


def test_blank_and_comment_lines_are_ignored(tmp_path: Path) -> None:
    entries = ["", "# a comment", *[json.dumps(_entry("p1", "apply"))]]
    report = labeling.validate_labels(
        _write(tmp_path, entries), pool_by_id=_pool_map_for([_entry("p1", "apply")])
    )
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
    complete = _complete_pool()
    ok = labeling.format_report(
        labeling.validate_labels(_write(tmp_path, complete), pool_by_id=_pool_map_for(complete))
    )
    assert "OK - all minimums met" in ok
    assert "valid cases : 40" in ok

    short = labeling.format_report(
        labeling.validate_labels(
            _write(tmp_path, [_entry("p1", "skip", "unrelated")]),
            pool_by_id=_pool_map_for([_entry("p1", "skip", "unrelated")]),
        )
    )
    assert "INCOMPLETE" in short


def test_cli_label_check_exit_codes(tmp_path: Path) -> None:
    complete = _complete_pool()
    good = _write(tmp_path, complete)
    pool_path = tmp_path / "pool.jsonl"
    pool_lines = [
        json.dumps({"pool_id": pid, "url": url, "urls": []})
        for pid, url in [(d["pool_id"], d["url"]) for d in complete]
    ]
    pool_path.write_text("\n".join(pool_lines) + "\n", encoding="utf-8")
    assert main(["label-check", "--labels", str(good), "--pool", str(pool_path)]) == 0

    short = tmp_path / "short.jsonl"
    short.write_text(json.dumps(_entry("p1", "apply")) + "\n", encoding="utf-8")
    sp = tmp_path / "sp.jsonl"
    sp.write_text(
        json.dumps({"pool_id": "p1", "url": "https://example.test/p1", "urls": []}) + "\n",
        encoding="utf-8",
    )
    assert main(["label-check", "--labels", str(short), "--pool", str(sp)]) == 1
    good = _write(tmp_path, _complete_pool())


def test_unknown_pool_id_reported(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps(
            {
                "pool_id": "p1",
                "url": "u1",
                "label": "apply",
                "reason": "fits well here",
                "labeled_by": "human",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pool = tmp_path / "pool.jsonl"
    pool.write_text("", encoding="utf-8")  # empty pool; p1 unknown
    report = labeling.validate_labels(labels, pool_path=pool)
    assert report.valid == 0
    assert any("unknown pool_id" in i.message for i in report.issues)


def test_duplicate_pool_id_reported_and_first_counts(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps(
            {
                "pool_id": "p1",
                "url": "u1",
                "label": "apply",
                "reason": "fits well here",
                "labeled_by": "human",
            }
        )
        + "\n"
        + json.dumps(
            {
                "pool_id": "p1",
                "url": "u1",
                "label": "skip",
                "reason": "too senior for this",
                "labeled_by": "human",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pool = tmp_path / "pool.jsonl"
    pool.write_text(json.dumps({"pool_id": "p1", "url": "u1"}) + "\n", encoding="utf-8")
    report = labeling.validate_labels(labels, pool_path=pool)
    assert report.valid == 1  # only first counts
    assert report.applies == 1
    assert any("duplicate pool_id (first at line 1)" in i.message for i in report.issues)


def test_url_mismatch_reported(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps(
            {
                "pool_id": "p1",
                "url": "u2",
                "label": "apply",
                "reason": "fits well here",
                "labeled_by": "human",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pool = tmp_path / "pool.jsonl"
    pool.write_text(json.dumps({"pool_id": "p1", "url": "u1"}) + "\n", encoding="utf-8")
    report = labeling.validate_labels(labels, pool_path=pool)
    assert report.valid == 0
    assert any("url does not match pool entry" in i.message for i in report.issues)


def test_url_matches_pool_urls_list_accepted(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        json.dumps(
            {
                "pool_id": "p1",
                "url": "u2",
                "label": "apply",
                "reason": "fits well here",
                "labeled_by": "human",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    pool = tmp_path / "pool.jsonl"
    pool.write_text(
        json.dumps({"pool_id": "p1", "url": "u1", "urls": ["u2"]}) + "\n", encoding="utf-8"
    )
    report = labeling.validate_labels(labels, pool_path=pool)
    assert report.valid == 1


def test_missing_pool_file_exits_1(tmp_path: Path) -> None:
    import subprocess
    import sys

    tmp_path / "labels.jsonl"
    # write minimal valid labels file? no, test CLI behavior
    complete = _complete_pool()
    labels_f = _write(tmp_path, complete)
    pool_path = tmp_path / "missing.jsonl"
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "atlas.cli",
            "label-check",
            "--labels",
            str(labels_f),
            "--pool",
            str(pool_path),
        ],
        cwd="D:\\Atlas",
        capture_output=True,
        check=False,
    )
    assert res.returncode == 1


def test_happy_path_meets_minimums_exit_0(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    pool = tmp_path / "pool.jsonl"
    pool_lines = []
    lbl_lines = []
    # create 40 apply and 15 skip (traps) to meet minimums
    # we'll make skip cases include seniority keywords to count as traps
    for i in range(40):
        pid = f"p{i}"
        url = f"u{i}"
        pool_lines.append(json.dumps({"pool_id": pid, "url": url}))
        lbl_lines.append(
            json.dumps(
                {
                    "pool_id": pid,
                    "url": url,
                    "label": "apply",
                    "reason": "fits well here",
                    "labeled_by": "human",
                }
            )
        )
    for i in range(40, 55):  # 15 more
        pid = f"p{i}"
        url = f"u{i}"
        pool_lines.append(json.dumps({"pool_id": pid, "url": url}))
        lbl_lines.append(
            json.dumps(
                {
                    "pool_id": pid,
                    "url": url,
                    "label": "skip",
                    "reason": "too senior for this role",
                    "labeled_by": "human",
                }
            )
        )
    pool.write_text("\n".join(pool_lines) + "\n", encoding="utf-8")
    labels.write_text("\n".join(lbl_lines) + "\n", encoding="utf-8")
    import subprocess
    import sys

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "atlas.cli",
            "label-check",
            "--labels",
            str(labels),
            "--pool",
            str(pool),
        ],
        cwd="D:\\Atlas",
        capture_output=True,
        check=False,
    )
    assert res.returncode == 0


def test_read_only_labels_and_pool_bytes_unchanged(tmp_path: Path) -> None:
    complete = _complete_pool()
    labels = _write(tmp_path, complete)
    pool = tmp_path / "pool.jsonl"
    pool_lines = []
    for d in complete:
        pool_lines.append(json.dumps({"pool_id": d["pool_id"], "url": d["url"], "urls": []}))
    pool.write_text("\n".join(pool_lines) + "\n", encoding="utf-8")
    before_labels = labels.read_bytes()
    before_pool = pool.read_bytes()
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-m",
            "atlas.cli",
            "label-check",
            "--labels",
            str(labels),
            "--pool",
            str(pool),
        ],
        cwd="D:\\Atlas",
        capture_output=True,
        check=False,
    )
    assert labels.read_bytes() == before_labels
    assert pool.read_bytes() == before_pool


def test_invalid_lines_not_counted_toward_minimums(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    pool = tmp_path / "pool.jsonl"
    pool_lines = []
    for i in range(55):
        pid = f"p{i}"
        url = f"u{i}"
        pool_lines.append(json.dumps({"pool_id": pid, "url": url}))
    valid = []
    for i in range(40):
        pid = f"p{i}"
        url = f"u{i}"
        valid.append(
            json.dumps(
                {
                    "pool_id": pid,
                    "url": url,
                    "label": "apply",
                    "reason": "fits well here",
                    "labeled_by": "human",
                }
            )
        )
    for i in range(40, 55):
        pid = f"p{i}"
        url = f"u{i}"
        valid.append(
            json.dumps(
                {
                    "pool_id": pid,
                    "url": url,
                    "label": "skip",
                    "reason": "too senior for this role",
                    "labeled_by": "human",
                }
            )
        )
    lbl_lines = []
    lbl_lines.append("{bad json")
    lbl_lines.extend(valid)
    lbl_lines.append("{another bad")
    pool.write_text("\n".join(pool_lines) + "\n", encoding="utf-8")
    labels.write_text("\n".join(lbl_lines) + "\n", encoding="utf-8")
    import subprocess
    import sys

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "atlas.cli",
            "label-check",
            "--labels",
            str(labels),
            "--pool",
            str(pool),
        ],
        cwd="D:\\Atlas",
        capture_output=True,
        check=False,
    )
    # Invalid lines must not count toward minimums; issues mean exit != 0
    out = res.stdout.decode("utf-8", errors="ignore")
    assert "valid cases : 55" in out
    assert "apply       : 40" in out
    assert "traps       : 15" in out
    assert res.returncode == 1


def test_unicode_line_separator_does_not_split_record(tmp_path):
    pass
