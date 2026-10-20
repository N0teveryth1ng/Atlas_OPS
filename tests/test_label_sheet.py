"""Tests for ``label-sheet`` and ``label-import`` (offline labeling workflow).

Every session uses a tmp pool and a tmp labels file driven by scripted fake
input; a dedicated test snapshots the real ``eval/labeling_pool.jsonl`` and
``eval/labels.jsonl`` to prove they stay byte-for-byte untouched. ``label-import``
must refuse non-interactive input (both streams must be a tty), reject bad rows,
require a typed "yes" read from the terminal and write exactly the records
``label-check`` validates - nothing is ever appended without the confirmation.
Successful imports append an audit line to ``eval/label_imports.log`` (redirected
to tmp in every test here).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

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


class _Stdout:
    """Fake stdout with a controllable ``isatty`` and captured text."""

    def __init__(self, tty: bool) -> None:
        self.tty = tty
        self.text: list[str] = []

    def isatty(self) -> bool:
        return self.tty

    def write(self, text: str) -> int:
        self.text.append(text)
        return len(text)

    def flush(self) -> None:
        pass


def _no_input(*_args, **_kwargs):
    raise AssertionError("input() must not be called when a stream is not a tty")


@pytest.fixture(autouse=True)
def _tmp_import_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "label_imports.log"
    monkeypatch.setattr(labeling_cli, "IMPORT_LOG_PATH", log)
    return log


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


def _run_sheet(pool: list[dict], labels: Path, count: int, **kwargs: object) -> tuple[int, str]:
    out: list[str] = []
    rc = labeling_cli.run_label_sheet(pool, labels, count, print_fn=out.append, **kwargs)
    return rc, "\n".join(out)


def _run_import(pool: list[dict], labels: Path, tsv: Path, *keys: str) -> tuple[int, str]:
    out: list[str] = []
    rc = labeling_cli._import_with_prompt(pool, labels, tsv, read=_Keys(*keys), say=out.append)
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
    assert (
        "label sheet: pool 12  labeled 0  pending 12  matched 12  showing 3 (fixed seed 20261018)"
        in out
    )
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
    assert "labeled 1  pending 5  matched 5  showing 5" in out
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
    assert "no pending jobs matched." in empty_out


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


def test_sheet_where_matches_location_title_and_description(tmp_path: Path) -> None:
    entries = [
        _entry(0),
        _entry(1, description="We build services in FastAPI and Postgres for remote teams."),
        _entry(2),
    ]
    entries[1]["location"] = "India"
    entries[2]["title"] = "Backend FastAPI Engineer"
    entries[2]["description"] = "Nothing special here."
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))

    _, fastapi_out = _run_sheet(pool, tmp_path / "labels.jsonl", 10, where=["fastapi"])
    assert "matched 2" in fastapi_out
    assert len(_field_lines(fastapi_out, "short_id : ")) == 2

    _, india_out = _run_sheet(pool, tmp_path / "labels.jsonl", 10, where=["INDIA"])
    assert len(_field_lines(india_out, "short_id : ")) == 1
    assert entries[1]["pool_id"][:8] in india_out


def test_sheet_where_is_case_insensitive_and_all_must_match(tmp_path: Path) -> None:
    entries = [
        _entry(0, description="Python backend role in Berlin."),
        _entry(1, description="Python data role in Berlin."),
        _entry(2, description="Java backend role in Berlin."),
    ]
    for entry in entries:
        entry["title"] = "Engineer"
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 10, where=["PYTHON", "Backend"])
    assert "matched 1" in out
    assert entries[0]["pool_id"][:8] in out
    assert entries[1]["pool_id"][:8] not in out
    assert entries[2]["pool_id"][:8] not in out


def test_sheet_exclude_drops_matches(tmp_path: Path) -> None:
    entries = [
        _entry(0, description="Python backend role in Berlin."),
        _entry(1, description="Python backend role in the United States."),
        _entry(2, description="Python backend role in India."),
    ]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 10, exclude=["United States", "Java"])
    assert "matched 2" in out
    assert entries[1]["pool_id"][:8] not in out
    assert entries[0]["pool_id"][:8] in out
    assert entries[2]["pool_id"][:8] in out


def test_sheet_remote_open_keeps_worldwide_and_drops_single_country(tmp_path: Path) -> None:
    entries = [
        _entry(0),
        _entry(1),
        _entry(2),
        _entry(3),
        _entry(4),
        _entry(5),
    ]
    entries[0]["location"] = "Worldwide"
    entries[1]["location"] = "India"
    entries[2]["location"] = "United States"
    entries[3]["location"] = "Canada"
    entries[4]["location"] = None
    entries[5]["location"] = "Canada, Mexico, USA"
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 10, remote_open=True)
    assert "matched 4" in out
    shown = _field_lines(out, "short_id : ")
    assert entries[0]["pool_id"][:8] in shown
    assert entries[1]["pool_id"][:8] in shown
    assert entries[2]["pool_id"][:8] not in shown
    assert entries[3]["pool_id"][:8] not in shown
    assert entries[4]["pool_id"][:8] in shown
    assert entries[5]["pool_id"][:8] in shown


def test_sheet_sort_recent_orders_by_posted_date(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(4)]
    entries[0]["posted_at"] = "2026-10-01T00:00:00+00:00"
    entries[1]["posted_at"] = "2026-10-18T00:00:00+00:00"
    entries[2]["posted_at"] = "2026-10-05T00:00:00+00:00"
    entries[3]["posted_at"] = "2026-10-12T00:00:00+00:00"
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    _, out = _run_sheet(pool, tmp_path / "labels.jsonl", 4, sort="recent")
    shown = _field_lines(out, "short_id : ")
    expected = [
        entries[1]["pool_id"][:8],
        entries[3]["pool_id"][:8],
        entries[2]["pool_id"][:8],
        entries[0]["pool_id"][:8],
    ]
    assert shown == expected


def test_sheet_default_output_is_unchanged_without_filters(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(5)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    default_rc, default_out = _run_sheet(pool, tmp_path / "labels.jsonl", 5)
    noop_rc, noop_out = _run_sheet(
        pool, tmp_path / "labels.jsonl", 5, where=[], exclude=[], remote_open=False, sort="seed"
    )
    assert default_rc == noop_rc == 0
    assert default_out == noop_out
    assert "matched 5" in default_out
    assert "showing 5" in default_out


def test_sheet_filters_never_show_labeled_jobs(tmp_path: Path) -> None:
    entries = [_entry(i, description="Python backend role worldwide.") for i in range(4)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    _seed_labels(labels, entries, 2)
    before = labels.read_bytes()
    _, out = _run_sheet(pool, labels, 10, where=["python"], remote_open=True)
    assert "matched 2" in out
    assert len(_field_lines(out, "short_id : ")) == 2
    assert entries[0]["pool_id"][:8] not in out
    assert entries[1]["pool_id"][:8] not in out
    assert labels.read_bytes() == before


def test_sheet_filters_are_read_only(tmp_path: Path) -> None:
    entries = [_entry(i) for i in range(3)]
    pool_path = _write_pool(tmp_path, entries)
    pool = labeling_cli.load_pool(pool_path)
    pool_before = pool_path.read_bytes()
    labels = tmp_path / "labels.jsonl"
    _run_sheet(pool, labels, 10, where=["python"], exclude=["senior"], remote_open=True)
    assert pool_path.read_bytes() == pool_before
    assert not labels.exists()


def test_sheet_filter_flags_wire_through_main(tmp_path: Path) -> None:
    entries = [
        _entry(0, description="Python backend role in Berlin."),
        _entry(1, description="Python backend role in the United States."),
    ]
    for entry in entries:
        entry["title"] = "Engineer"
        entry["location"] = "Worldwide"
    entries[1]["location"] = "United States"
    pool_path = _write_pool(tmp_path, entries)
    labels = tmp_path / "labels.jsonl"
    rc = main(
        [
            "label-sheet",
            "--count",
            "10",
            "--pool",
            str(pool_path),
            "--labels",
            str(labels),
            "--where",
            "python",
            "--exclude",
            "united states",
            "--remote-open",
            "--sort",
            "recent",
        ]
    )
    assert rc == 0
    assert not labels.exists()


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
    monkeypatch.setattr("builtins.input", _no_input)
    rc = main(
        ["label-import", str(tsv), "--pool", str(tmp_path / "pool.jsonl"), "--labels", str(labels)]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "refuses non-interactive input" in captured.err
    assert not labels.exists()


def test_import_refuses_when_only_stdout_is_not_a_terminal(tmp_path, monkeypatch, capsys) -> None:
    entries = [_entry(i) for i in range(3)]
    pool_path = _write_pool(tmp_path, entries)
    labels = tmp_path / "labels.jsonl"
    tsv = _write_tsv(tmp_path, [f"{entries[0]['pool_id'][:8]}\ta\tstrong python match"])
    monkeypatch.setattr("sys.stdin", _Stdin(True))
    monkeypatch.setattr("sys.stdout", _Stdout(False))
    monkeypatch.setattr("builtins.input", _no_input)
    rc = main(["label-import", str(tsv), "--pool", str(pool_path), "--labels", str(labels)])
    captured = capsys.readouterr()
    assert rc == 2
    assert "refuses non-interactive input" in captured.err
    assert not labels.exists()


def test_module_function_cannot_write_without_a_terminal(tmp_path, monkeypatch, capsys) -> None:
    """Calling the module function directly must refuse, not just the CLI."""
    entries = [_entry(i) for i in range(3)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    tsv = _write_tsv(tmp_path, [f"{entries[0]['pool_id'][:8]}\ta\tstrong python match"])
    monkeypatch.setattr("sys.stdin", _Stdin(False))
    monkeypatch.setattr("builtins.input", _no_input)
    rc = labeling_cli.run_label_import(pool, labels, tsv)
    captured = capsys.readouterr()
    assert rc == 2
    assert "refuses non-interactive input" in captured.err
    assert not labels.exists()


def test_module_function_with_a_terminal_still_needs_the_typed_yes(tmp_path, monkeypatch) -> None:
    entries = [_entry(i) for i in range(3)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    tsv = _write_tsv(tmp_path, [f"{entries[0]['pool_id'][:8]}\ta\tstrong python match"])
    stdout = _Stdout(True)
    monkeypatch.setattr("sys.stdin", _Stdin(True))
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("builtins.input", _Keys("y"))
    rc = labeling_cli.run_label_import(pool, labels, tsv)
    out = "".join(stdout.text)
    assert rc == 1
    assert 'not "yes"' in out
    assert not labels.exists()


def test_import_wiring_through_main_with_tty_and_yes(tmp_path, monkeypatch) -> None:
    entries = [_entry(i) for i in range(5)]
    pool_path = _write_pool(tmp_path, entries)
    labels = tmp_path / "labels.jsonl"
    pending = labeling_cli.pending_pool(labeling_cli.load_pool(pool_path), labels)
    tsv = _write_tsv(
        tmp_path,
        [
            f"{str(pending[0]['pool_id'])[:8]}\ta\tstrong python match",
            f"{str(pending[1]['pool_id'])[:8]}\ts\tsenior title",
        ],
    )
    stdout = _Stdout(True)
    monkeypatch.setattr("sys.stdin", _Stdin(True))
    monkeypatch.setattr("sys.stdout", stdout)
    scripted = _Keys("yes")
    monkeypatch.setattr("builtins.input", scripted)
    rc = main(["label-import", str(tsv), "--pool", str(pool_path), "--labels", str(labels)])
    out = "".join(stdout.text)
    assert rc == 1
    assert "label import:" in out
    assert any('type "yes" to append' in prompt for prompt in scripted.prompts)
    assert "tally: valid 2" in out
    assert len(_records(labels)) == 2
    assert len(labeling_cli.IMPORT_LOG_PATH.read_text(encoding="utf-8").splitlines()) == 1


def test_import_appends_an_audit_line_with_timestamp_name_sha256_and_rows(tmp_path) -> None:
    entries = [_entry(i) for i in range(4)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    pending = labeling_cli.pending_pool(pool, labels)
    tsv = _write_tsv(
        tmp_path,
        [
            f"{str(pending[0]['pool_id'])[:8]}\ta\treason one",
            f"{str(pending[1]['pool_id'])[:8]}\ts\tsenior title",
        ],
    )
    rc, _ = _run_import(pool, labels, tsv, "yes")
    assert rc == 1

    lines = labeling_cli.IMPORT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stamp, name, digest, rows = lines[0].split("\t")
    assert datetime.fromisoformat(stamp).tzinfo is not None
    assert name == tsv.name
    assert digest == hashlib.sha256(tsv.read_bytes()).hexdigest()
    assert rows == "2"

    second = _write_tsv(
        tmp_path, [f"{str(pending[2]['pool_id'])[:8]}\ta\tsecond import"], name="b2.tsv"
    )
    _run_import(pool, labels, second, "yes")
    lines = labeling_cli.IMPORT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[1].split("\t")[1] == "b2.tsv"


def test_rejected_import_writes_no_audit_line(tmp_path) -> None:
    entries = [_entry(i) for i in range(3)]
    pool = labeling_cli.load_pool(_write_pool(tmp_path, entries))
    labels = tmp_path / "labels.jsonl"
    tsv = _write_tsv(tmp_path, ["zzzzzzzz\ta\tsome reason"])
    rc, _ = _run_import(pool, labels, tsv, "yes")
    assert rc == 1
    assert not labels.exists()
    assert not labeling_cli.IMPORT_LOG_PATH.exists()


def test_import_never_touches_the_real_eval_files(tmp_path, monkeypatch) -> None:
    def snapshot(path: Path) -> tuple[bool, bytes]:
        return (path.exists(), path.read_bytes() if path.exists() else b"")

    before = (snapshot(REAL_POOL), snapshot(REAL_LABELS))
    entries = [_entry(i) for i in range(3)]
    pool_path = _write_pool(tmp_path, entries)
    labels = tmp_path / "labels.jsonl"
    tsv = _write_tsv(tmp_path, [f"{entries[0]['pool_id'][:8]}\ta\tstrong python match"])
    stdout = _Stdout(True)
    monkeypatch.setattr("sys.stdin", _Stdin(True))
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("builtins.input", _Keys("yes"))
    rc = main(["label-import", str(tsv), "--pool", str(pool_path), "--labels", str(labels)])
    assert rc == 1
    assert len(_records(labels)) == 1
    assert (snapshot(REAL_POOL), snapshot(REAL_LABELS)) == before
