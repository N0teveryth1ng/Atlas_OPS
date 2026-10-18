"""Tests for the interactive pool-labeling session (``python -m atlas.cli label``).

Rules under test: nothing is written except on an owner keypress, records use
exactly the keys ``label-check`` validates, already-labeled pool ids are never
shown again (resumable), undo rewrites only the last label, no score/decision/
filter output is ever displayed, and the real ``eval/labeling_pool.jsonl`` plus
``eval/labels.jsonl`` stay byte-for-byte untouched (all sessions use tmp files).
"""

from __future__ import annotations

import json
from pathlib import Path

from atlas import labeling, labeling_cli
from atlas.cli import main
from atlas.config import REPO_ROOT

REAL_POOL = REPO_ROOT / "eval" / "labeling_pool.jsonl"
REAL_LABELS = REPO_ROOT / "eval" / "labels.jsonl"

TITLE_PREFIX = "  title    : "


class _Keys:
    """Fake ``input``: returns scripted keystrokes, then raises EOFError."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.keys:
            raise EOFError("no more keys")
        return self.keys.pop(0)


def _pool_entry(i: int) -> dict:
    return {
        "pool_id": f"pool-{i:03d}",
        "source": "remotive",
        "source_id": str(i),
        "title": f"Junior Python Developer {i}",
        "company": f"Company {i}",
        "location": "Remote",
        "remote_type": "remote",
        "url": f"https://example.test/jobs/{i}",
        "urls": [f"https://example.test/jobs/{i}"],
        "posted_at": "2026-10-10T00:00:00+00:00",
        "fetched_at": "2026-10-18T00:00:00+00:00",
        "description": f"Description for job {i}. Build things with Python.",
    }


def _write_pool(tmp_path: Path, n: int, name: str = "pool.jsonl") -> tuple[Path, list[dict]]:
    entries = [_pool_entry(i) for i in range(n)]
    path = tmp_path / name
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, entries


def _run(pool: list[dict], labels: Path, keys: tuple[str, ...]) -> tuple[int, str, _Keys]:
    out: list[str] = []
    scripted = _Keys(*keys)
    rc = labeling_cli.run_label_session(pool, labels, input_fn=scripted, print_fn=out.append)
    return rc, "\n".join(out), scripted


def _records(labels: Path) -> list[dict]:
    text = labels.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _titles(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(TITLE_PREFIX)]


def test_quit_writes_nothing_and_runs_final_check(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 3)
    labels = tmp_path / "labels.jsonl"
    rc, out, scripted = _run(pool, labels, ("q",))
    assert rc == 1
    assert not labels.exists()
    assert len(scripted.prompts) == 1
    assert "final check (same as label-check):" in out
    assert "line 0: labels file not found" in out
    assert "need 40 valid cases (have 0)" in out


def test_records_use_exactly_the_label_check_keys(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 3)
    labels = tmp_path / "labels.jsonl"
    rc, _, _ = _run(pool, labels, ("a", "strong python match", "s", "senior title", "q"))
    records = _records(labels)
    assert [r["label"] for r in records] == ["apply", "skip"]
    assert records[1]["reason"] == "senior title"
    for record in records:
        assert list(record) == ["pool_id", "url", "label", "reason", "labeled_by"]
        assert record["labeled_by"] == "human"
    report = labeling.validate_labels(labels, pool_by_id={e["pool_id"]: e for e in pool})
    assert report.valid == 2
    assert report.applies == 1
    assert report.skips == 1
    assert report.traps == 1
    assert report.issues == []
    assert rc == 1


def test_shows_job_fields_but_no_score_or_decision(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 4)
    labels = tmp_path / "labels.jsonl"
    _, out, _ = _run(pool, labels, ("q",))
    titles = _titles(out)
    assert len(titles) == 1
    shown = next(e for e in pool if e["title"] == titles[0][len(TITLE_PREFIX) :])
    for field in (
        shown["title"],
        shown["company"],
        shown["location"],
        shown["remote_type"],
        shown["source"],
        shown["posted_at"][:10],
        shown["url"],
        shown["description"],
    ):
        assert field in out
    lowered = out.lower()
    for forbidden in (
        "fit_score",
        "score",
        "verdict",
        "decision",
        "confidence",
        "recommendation",
        "rank",
        "missing",
        "vetoe",
    ):
        assert forbidden not in lowered
    for entry in pool:
        for key in ("fit_score", "decision", "score", "verdict"):
            assert key not in entry


def test_skip_shows_all_seniority_hints(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 2)
    labels = tmp_path / "labels.jsonl"
    _, out, _ = _run(pool, labels, ("s", "wrong stack", "q"))
    assert "skip hints: " in out
    for hint in labeling_cli.SKIP_HINTS:
        assert hint in out


def test_empty_reason_is_rejected_and_reprompted(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 2)
    labels = tmp_path / "labels.jsonl"
    _, out, _ = _run(pool, labels, ("s", "", "wrong stack", "q"))
    assert "reason must be one non-empty line." in out
    assert _records(labels)[0]["reason"] == "wrong stack"


def test_unknown_key_reprompts_the_same_job(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 2)
    labels = tmp_path / "labels.jsonl"
    rc, out, _ = _run(pool, labels, ("x", "q"))
    assert "unknown key - use a, s, u or q." in out
    titles = _titles(out)
    assert titles[0] == titles[1]
    assert not labels.exists()
    assert rc == 1


def test_undo_removes_the_last_label_and_reshows_that_job(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 3)
    labels = tmp_path / "labels.jsonl"
    _, out, _ = _run(pool, labels, ("a", "good match", "u", "q"))
    assert "undid 1 label(s)" in out
    assert labels.read_text(encoding="utf-8").strip() == ""
    titles = _titles(out)
    assert titles.count(titles[0]) == 2


def test_undo_on_empty_labels_file_is_reported_not_crashed(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 2)
    labels = tmp_path / "labels.jsonl"
    rc, out, _ = _run(pool, labels, ("u", "q"))
    assert "nothing to undo - the labels file is empty." in out
    assert not labels.exists()
    assert rc == 1


def test_resume_never_relabels_a_pool_id(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 3)
    labels = tmp_path / "labels.jsonl"
    _run(pool, labels, ("a", "match one", "q"))
    first = _records(labels)
    assert len(first) == 1
    labeled_entry = next(e for e in pool if e["pool_id"] == first[0]["pool_id"])

    rc, out, _ = _run(pool, labels, ("s", "wrong stack", "q"))
    assert "1 already labeled" in out
    assert labeled_entry["title"] not in out
    records = _records(labels)
    assert len(records) == 2
    assert len({r["pool_id"] for r in records}) == 2
    report = labeling.validate_labels(labels, pool_by_id={e["pool_id"]: e for e in pool})
    assert report.valid == 2
    assert report.issues == []
    assert rc == 1


def test_live_tally_progress_and_shortfalls(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 2)
    labels = tmp_path / "labels.jsonl"
    _, out, _ = _run(pool, labels, ("s", "senior title", "q"))
    assert "tally: valid 0  apply 0  skip 0  traps 0" in out
    assert "still needed: 40 valid, 15 traps, 10 apply" in out
    assert "tally: valid 1  apply 0  skip 1  traps 1" in out
    assert "still needed: 39 valid, 14 traps, 10 apply" in out
    assert "progress: pool 2  labeled 1  this session 1  left 1" in out
    assert f"label-check: {labels}" in out
    assert "result      : INCOMPLETE" in out
    assert "need 40 valid cases (have 1)" in out


def test_fixed_seed_shows_a_stable_shuffled_order(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 12, name="pool.jsonl")
    keys = tuple(k for _ in pool for k in ("a", "match"))
    _, out_a, _ = _run(pool, tmp_path / "a.jsonl", keys)
    _, out_b, _ = _run(pool, tmp_path / "b.jsonl", keys)
    titles_a = _titles(out_a)
    titles_b = _titles(out_b)
    assert titles_a == titles_b
    assert len(titles_a) == 12
    assert titles_a != [TITLE_PREFIX + e["title"] for e in pool]
    assert len({title[len(TITLE_PREFIX) :] for title in titles_a}) == 12


def test_eof_mid_reason_writes_nothing(tmp_path: Path) -> None:
    _, pool = _write_pool(tmp_path, 2)
    labels = tmp_path / "labels.jsonl"
    rc, out, _ = _run(pool, labels, ("a",))
    assert not labels.exists()
    assert "input closed - quitting (progress is saved)." in out
    assert rc == 1


def test_cli_label_wires_through_main(tmp_path: Path, monkeypatch, capsys) -> None:
    path, _ = _write_pool(tmp_path, 3)
    labels = tmp_path / "labels.jsonl"
    monkeypatch.setattr("builtins.input", _Keys("a", "good match", "q"))
    rc = main(["label", "--pool", str(path), "--labels", str(labels)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "final check (same as label-check):" in captured.out
    assert len(_records(labels)) == 1


def test_cli_label_missing_or_empty_pool_returns_2(tmp_path, capsys) -> None:
    labels = tmp_path / "labels.jsonl"
    missing = main(["label", "--pool", str(tmp_path / "nope.jsonl"), "--labels", str(labels)])
    assert missing == 2
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert main(["label", "--pool", str(empty), "--labels", str(labels)]) == 2
    assert not labels.exists()


def test_real_eval_files_are_untouched(tmp_path: Path) -> None:
    def snapshot(path: Path) -> tuple[bool, bytes]:
        return (path.exists(), path.read_bytes() if path.exists() else b"")

    before = (snapshot(REAL_POOL), snapshot(REAL_LABELS))
    _, pool = _write_pool(tmp_path, 4)
    labels = tmp_path / "labels.jsonl"
    _run(pool, labels, ("a", "match", "s", "wrong stack", "u", "q"))
    after = (snapshot(REAL_POOL), snapshot(REAL_LABELS))
    assert after == before
    assert labels.exists()
    assert str(REAL_LABELS) not in labels.read_text(encoding="utf-8")
