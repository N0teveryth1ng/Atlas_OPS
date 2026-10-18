"""Tests for ``label-sheet`` and ``label-import`` (offline labeling workflow).

Every session uses a tmp pool and a tmp labels file driven by scripted fake
input; a dedicated test snapshots the real ``eval/labeling_pool.jsonl`` and
``eval/labels.jsonl`` to prove they stay byte-for-byte untouched. ``label-import``
must refuse non-interactive input, reject bad rows, require a typed "yes" and
write exactly the records ``label-check`` validates - nothing is ever appended
without the confirmation.
"""

from __future__ import annotations

import json
from pathlib import Path

from atlas import labeling, labeling_cli
from atlas.cli import main
from atlas.config import REPO_ROOT

REAL_POOL = REPO_ROOT / "eval" / "labeling_pool.jsonl"
REAL_LABELS = REPO_ROOT / "eval" / "labels.jsonl"

SHEET_TITLE_PREFIX = "title    : "


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


class _Stdin:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def _entry(i: int, description: str | None = None) -> dict:
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
        "description": description or f"Description for job {i}. Build things with Python.",
    }


def _write_pool(tmp_path: Path, entries: list[dict], name: str = "pool.jsonl") -> Path:
    path = tmp_path / name
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_tsv(
    tmp_path: Path, rows: list[str], header: str | None = None, name: str = "in.tsv"
) -> Path:
    path = tmp_path / name
    head = header if header is not None else "short_id\tlabel\treason"
    path.write_text(head + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _records(labels: Path) -> list[dict]:
    if not labels.exists():
        return []
    return [
        json.loads(line) for line in labels.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _seed_labels(labels: Path, entries: list[dict], count: int) -> list[dict]:
    """Pre-label the first ``count`` entries directly (owner-owned file fixture)."""
    seeded = []
    for entry in entries[:count]:
        record = {
            "pool_id": entry["pool_id"],
            "url": entry["url"],
            "label": "apply",
            "reason": "already done",
            "labeled_by": "human",
        }
        labels.write_text(
            "".join(json.dumps(r) + "\n" for r in seeded + [record]),
            encoding="utf-8",
            newline="\n",
        )
        seeded.append(record)
    return seeded


def _run_sheet(pool: list[dict], labels: Path, count: int) -> tuple[int, str]:
    out: list[str] = []
    rc = labeling_cli.run_label_sheet(pool, labels, count, print_fn=out.append)
    return rc, "\n".join(out)


def _run_import(pool: list[dict], labels: Path, tsv: Path, *keys: str) -> tuple[int, str]:
    out: list[str] = []
    rc = labeling_cli.run_label_import(
        pool, labels, tsv, input_fn=_Keys(*keys), print_fn=out.append
    )
    return rc, "\n".join(out)


def _field_lines(output: str, prefix: str) -> list[str]:
    return [line[len(prefix) :] for line in output.splitlines() if line.startswith(prefix)]


# --------------------------------------------------------------------------- #
# label-sheet
# --------------------------------------------------------------------------- #


def test_sheet_walks_the_same_fixed_seed_order_as_label(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(12)]
    pool_path = _write_pool(tmp_path, entries)
    pool = labeling_cli.load_pool(pool_path)

    sheet_rc, sheet_out = _run_sheet(pool, tmp_path / "sheet_labels.jsonl", 12)
    assert sheet_rc == 0

    session_labels = tmp_path / "session_labels.jsonl"
    session_keys = tuple(k for _ in entries for k in ("a", "match"))
    session_out: list[str] = []
    labeling_cli.run_label_session(
        pool, session_labels, input_fn=_Keys(*session_keys), print_fn=session_out.append
    )

    sheet_titles = _field_lines(sheet_out, SHEET_TITLE_PREFIX)
    session_titles = _field_lines("\n".join(session_out), "  title    : ")
    assert sheet_titles == session_titles
    assert len(sheet_titles) == 12
    assert len(set(sheet_titles)) == 12


def test_sheet_shows_every_required_field_and_header(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(12)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    rc, out = _run_sheet(pool, tmp_path / "labels.jsonl", 3)
    assert rc == 0
    assert "label sheet: pool 12  labeled 0  pending 12  showing 3 (fixed seed 20261018)" in out
    assert "raw pool text only - nothing computed" in out
    job_lines = [line for line in out.splitlines() if line.startswith("job ")]
    assert len(job_lines) == 3
    shown_first = _field_lines(out, SHEET_TITLE_PREFIX)[0]
    entry = next(e for e in entries if e["title"] == shown_first)
    for prefix, value in (
        ("short_id : ", entry["pool_id"][:8]),
        (SHEET_TITLE_PREFIX, entry["title"]),
        ("company  : ", entry["company"]),
        ("location : ", entry["location"]),
        ("remote   : ", entry["remote_type"]),
        ("source   : ", entry["source"]),
        ("posted   : ", "2026-10-10"),
        ("url      : ", entry["url"]),
        ("digest   : ", entry["description"][:40]),
    ):
        assert prefix + value in out
    assert len(_field_lines(out, "short_id : ")) == 3


def test_sheet_skips_jobs_already_labeled(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(6)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    _seed_labels(labels, entries, 1)
    rc, out = _run_sheet(pool, labels, 10)
    assert rc == 0
    assert "labeled 1  pending 5  showing 5" in out
    assert entries[0]["pool_id"][:8] not in out
    assert len(_field_lines(out, "short_id : ")) == 5


def test_sheet_count_clamps_to_pending_and_rejects_zero(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(4)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    rc, out = _run_sheet(pool, labels, 50)
    assert rc == 0
    assert "showing 4" in out
    zero_rc, zero_out = _run_sheet(pool, labels, 0)
    assert zero_rc == 2
    assert "--count must be >= 1" in zero_out
    _seed_labels(labels, entries, 4)
    empty_rc, empty_out = _run_sheet(pool, labels, 5)
    assert empty_rc == 0
    assert "no pending jobs left to label." in empty_out


def test_sheet_digest_starts_with_prefix_and_caps_at_1300(tmp_path: Path) -> None:
    filler = "Alpha beta gamma delta epsilon zeta eta theta. " * 8
    pad = " lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod" * 4
    description = filler + "".join(
        f" Experience requirement number {i} applies here{pad} end." for i in range(8)
    )
    entry = _entry(0, description=description)
    pool = labeling_cli.load_pool(_write_pool(tmp_path, [entry]))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 1)
    digest = _field_lines(out, "digest   : ")[0]
    assert digest.startswith(description[:250])
    assert len(digest) == labeling_cli.DIGEST_MAX_CHARS
    assert digest.count("Experience requirement number") <= labeling_cli.DIGEST_MAX_SENTENCES


def test_sheet_digest_quotes_at_most_six_sentences(tmp_path: Path) -> None:
    filler = "Alpha beta gamma delta epsilon zeta eta theta. " * 8
    body = "".join(f" You require experience with topic {i} here. " for i in range(10))
    description = filler + body
    entry = _entry(0, description=description)
    pool = labeling_cli.load_pool(_write_pool(tmp_path, [entry]))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 1)
    digest = _field_lines(out, "digest   : ")[0]
    assert digest.count("You require experience with topic") == labeling_cli.DIGEST_MAX_SENTENCES
    assert len(digest) <= labeling_cli.DIGEST_MAX_CHARS


def test_sheet_digest_quotes_hint_and_technology_sentences(tmp_path: Path) -> None:
    filler = "Alpha beta gamma delta epsilon zeta eta theta iota kappa. " * 6
    description = (
        filler
        + " You need 5 years of experience with Python APIs. "
        + "Maintain Kubernetes clusters daily. "
        + "You will also own the deployment pipeline yourself. "
    )
    entry = _entry(0, description=description)
    pool = labeling_cli.load_pool(_write_pool(tmp_path, [entry]))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 1)
    digest = _field_lines(out, "digest   : ")[0]
    assert digest.startswith(description[:250])
    assert "5 years of experience" in digest
    assert "Kubernetes" in digest
    assert "own the deployment pipeline" in digest
    assert len(digest) <= labeling_cli.DIGEST_MAX_CHARS


def test_sheet_flags_non_english_only_when_heuristic_trips(tmp_path: Path) -> None:
    hindi = "यह एक नौकरी है जिसके लिए पांच वर्षों का अनुभव चाहिए और पाइथन आवश्यक है"
    entries = [_entry(0), _entry(1, description=hindi)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 2)
    blocks = out.split("\n\n")
    english_block = next(b for b in blocks if f"title    : {entries[0]['title']}" in b)
    hindi_block = next(b for b in blocks if f"title    : {entries[1]['title']}" in b)
    assert "[non-English?]" not in english_block
    assert "[non-English?]" in hindi_block
    assert out.count("[non-English?]") == 1


def test_sheet_contains_no_system_output(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(3)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 3)
    lowered = out.lower()
    for forbidden in (
        "fit_score",
        "score",
        "decision",
        "verdict",
        "confidence",
        "recommendation",
        "veto",
        "evaluator",
        "verifier",
        "braintrust",
        "result:",
        " info ",
        "warning",
        "run=-",
    ):
        assert forbidden not in lowered


def test_sheet_never_touches_the_real_eval_files(tmp_path: Path) -> None:
    """The CLI entry point must never create or modify the real eval files.

    Runs through ``main()`` with explicit tmp paths (the real pool is gitignored
    and absent in CI), then asserts both real files are byte-identical - or still
    absent - afterwards.
    """

    def snapshot(path: Path) -> tuple[bool, bytes]:
        return (path.exists(), path.read_bytes() if path.exists() else b"")

    before = (snapshot(REAL_POOL), snapshot(REAL_LABELS))
    entries = [_entry(i) for i in range(3)]
    pool_path = _write_pool(tmp_path, entries)
    rc = main(
        [
            "label-sheet",
            "--count",
            "2",
            "--pool",
            str(pool_path),
            "--labels",
            str(tmp_path / "labels.jsonl"),
        ]
    )
    after = (snapshot(REAL_POOL), snapshot(REAL_LABELS))
    assert rc == 0
    assert after == before


# --------------------------------------------------------------------------- #
# label-import
# --------------------------------------------------------------------------- #


def test_import_rejects_bad_rows_and_writes_nothing(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(6)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    _seed_labels(labels, entries, 1)
    before = labels.read_bytes()

    pending = labeling_cli.pending_pool(pool, labels)
    ids = [str(e["pool_id"])[:8] for e in pending]
    tsv = _write_tsv(
        tmp_path,
        [
            f"{ids[0]}\ta\tstrong python match",
            "zzzzzzzz\ta\tsome reason",
            f"{ids[1]}\tx\tbad label value",
            f"{ids[2]}\ta\t   ",
            f"{ids[0]}\ts\tduplicate of the first row",
        ],
    )
    rc, out = _run_import(pool, labels, tsv, "yes")
    assert rc == 1
    assert "rows: 5  ok: 1  bad: 4" in out
    assert "unknown short_id" in out
    assert "label must be a or s" in out
    assert "empty reason" in out
    assert "duplicate short_id" in out
    assert "validation failed - nothing was written." in out
    assert labels.read_bytes() == before


def test_import_rejects_already_labeled_short_id(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(4)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    _seed_labels(labels, entries, 1)
    before = labels.read_bytes()
    tsv = _write_tsv(tmp_path, [f"{entries[0]['pool_id'][:8]}\ta\tlate again"])
    rc, out = _run_import(pool, labels, tsv, "yes")
    assert rc == 1
    assert "already labeled" in out
    assert labels.read_bytes() == before


def test_import_requires_a_typed_yes(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(4)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    pending = labeling_cli.pending_pool(pool, labels)
    short_id = str(pending[0]["pool_id"])[:8]
    tsv = _write_tsv(tmp_path, [f"{short_id}\ta\tstrong python match"])

    rc, out = _run_import(pool, labels, tsv, "y")
    assert rc == 1
    assert 'not "yes"' in out
    assert not labels.exists()

    eof_rc, eof_out = _run_import(pool, labels, tsv)
    assert eof_rc == 1
    assert "input closed - aborted, nothing written." in eof_out
    assert not labels.exists()


def test_import_appends_exact_records_then_tally_and_report(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(5)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    pending = labeling_cli.pending_pool(pool, labels)
    first = str(pending[0]["pool_id"])[:8]
    second = str(pending[1]["pool_id"])[:8]
    tsv = _write_tsv(
        tmp_path,
        [
            f"{first}\ta\tstrong python match",
            f"{second}\ts\tsenior title",
        ],
    )
    rc, out = _run_import(pool, labels, tsv, "yes")
    records = _records(labels)
    assert len(records) == 2
    for record in records:
        assert list(record) == ["pool_id", "url", "label", "reason", "labeled_by"]
        assert record["labeled_by"] == "human"
    assert [r["label"] for r in records] == ["apply", "skip"]
    assert "appended 2 label(s)" in out
    assert "tally: valid 2  apply 1  skip 1  traps 1" in out
    assert "still needed: 38 valid, 14 traps, 9 apply" in out
    assert f"label-check: {labels}" in out
    assert "need 40 valid cases (have 2)" in out
    assert "result      : INCOMPLETE" in out
    assert rc == 1
    report = labeling.validate_labels(labels, pool_by_id={e["pool_id"]: e for e in pool})
    assert report.valid == 2
    assert report.issues == []


def test_import_unusable_tsv_returns_2(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(3)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"

    rc, out = _run_import(pool, labels, tmp_path / "missing.tsv")
    assert rc == 2
    assert "TSV file not found" in out

    bad_header = _write_tsv(tmp_path, ["whatever"], header="id\tlabel\tnote", name="bad.tsv")
    rc, out = _run_import(pool, labels, bad_header)
    assert rc == 2
    assert "header must be exactly: short_id\tlabel\treason" in out

    header_only = _write_tsv(tmp_path, [], name="empty.tsv")
    rc, out = _run_import(pool, labels, header_only)
    assert rc == 2
    assert "no data rows" in out
    assert not labels.exists()


def test_import_row_with_wrong_column_count_is_bad(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(3)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    pending = labeling_cli.pending_pool(pool, labels)
    short_id = str(pending[0]["pool_id"])[:8]
    tsv = _write_tsv(tmp_path, [f"{short_id}\ta"])
    rc, out = _run_import(pool, labels, tsv, "yes")
    assert rc == 1
    assert "expected 3 tab-separated columns, got 2" in out
    assert not labels.exists()


def test_import_refuses_non_interactive_input(tmp_path, monkeypatch, capsys) -> None:
    entries = [_entry(i) for i in range(3)]
    _write_pool(tmp_path, entries)
    labels = tmp_path / "labels.jsonl"
    tsv = _write_tsv(tmp_path, [f"{entries[0]['pool_id'][:8]}\ta\tstrong python match"])
    monkeypatch.setattr("sys.stdin", _Stdin(False))

    def _boom(*_args, **_kwargs):
        raise AssertionError("input() must not be called when stdin is not a tty")

    monkeypatch.setattr("builtins.input", _boom)
    rc = main(
        ["label-import", str(tsv), "--pool", str(tmp_path / "pool.jsonl"), "--labels", str(labels)]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "refuses non-interactive input" in captured.err
    assert not labels.exists()


def test_import_wiring_through_main_with_tty_and_yes(tmp_path, monkeypatch, capsys) -> None:
    entries = [_entry(i) for i in range(5)]
    pool_path = _write_pool(tmp_path, entries)
    labels = tmp_path / "labels.jsonl"
    pending = labeling_cli.pending_pool(labeling_cli.load_pool(pool_path), labels)
    tsv = _write_tsv(tmp_path, [f"{str(pending[0]['pool_id'])[:8]}\ta\tstrong python match"])
    monkeypatch.setattr("sys.stdin", _Stdin(True))
    scripted = _Keys("yes")
    monkeypatch.setattr("builtins.input", scripted)
    rc = main(["label-import", str(tsv), "--pool", str(pool_path), "--labels", str(labels)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "label import:" in captured.out
    assert any('type "yes" to append' in prompt for prompt in scripted.prompts)
    assert "tally: valid 1" in captured.out
    assert len(_records(labels)) == 1
