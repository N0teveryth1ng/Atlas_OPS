"""Interactive, resumable pool labelling: ``python -m atlas.cli label``.

The human drives every keystroke. The tool shows **one** unlabeled job from the
labeling pool at a time - title, company, location, remote type, source, posted
date, URL and the full description - in a random order drawn with a fixed seed,
and appends exactly one JSON object to the labels file per accepted label.

The same fixed-seed order also backs the offline pair:

* ``label-sheet --count N`` prints the next N pending jobs as compact text
  (short_id, the fields above, a capped description digest, an optional
  ``[non-English?]`` flag) so the owner can label away from the terminal. Raw-text
  filters narrow the set: ``--where``/``--exclude`` (repeatable, case-insensitive)
  and ``--remote-open``; ``--sort recent`` orders by post date instead of the seed.
* ``label-import FILE`` validates a TSV of those short_ids, shows a summary
  table, and appends to the labels file only after the owner types ``yes`` on a
  real terminal (both stdin and stdout must be a tty; there is no flag,
  environment variable or public function that skips the prompt). Every
  successful import appends an audit line (UTC timestamp, source file name,
  SHA-256, row count) to ``eval/label_imports.log``.

Deliberate properties:

* **No anchoring.** The pool itself is raw JD text, so no score, decision, hard
  filter result, ranking or model output is ever displayed or stored.
* **Writes only on a keypress.** Nothing is written while loading, resuming,
  tallying or running the final checks; ``u`` rewrites the file because the
  owner pressed it.
* **Resumable and idempotent.** A pool_id already present in the labels file is
  never shown again, so a session can be quit and continued at any time.
* **Same checks as ``label-check``.** The session ends by running
  :func:`atlas.labeling.validate_labels` and printing
  :func:`atlas.labeling.format_report`.

The labels file is human-owned: this module only ever appends/removes on an
explicit keypress from the person running it.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from .labeling import MIN_APPLY, MIN_CASES, MIN_TRAPS, format_report, validate_labels
from .skills import get_ontology

#: Fixed shuffle seed, so two people (or one person, twice) see the same order.
SHUFFLE_SEED = 20261018

#: Hints shown when the owner records a ``skip``.
SKIP_HINTS: tuple[str, ...] = (
    "senior title",
    "requires N years experience",
    "wrong stack",
    "wrong location",
    "not a real job",
    "hidden seniority (ownership, mentoring)",
)

#: The exact keys written, in this order - what ``label-check`` validates.
RECORD_KEYS: tuple[str, ...] = ("pool_id", "url", "label", "reason", "labeled_by")

#: Keypress -> stored label (the stored value is what ``label-check`` validates).
KEY_TO_LABEL: dict[str, str] = {"a": "apply", "s": "skip"}

#: ``label-sheet`` knobs.
SHORT_ID_LEN = 8
DIGEST_PREFIX_CHARS = 250
DIGEST_MAX_CHARS = 1300
DIGEST_MAX_SENTENCES = 6
NON_ENGLISH_MIN_NON_ASCII_LETTERS = 5
NON_ENGLISH_RATIO = 0.10

#: Columns of the TSV accepted by ``label-import``.
TSV_COLUMNS: tuple[str, ...] = ("short_id", "label", "reason")

#: Gitignored audit log: one line per successful import (no JD text).
IMPORT_LOG_PATH = Path("eval") / "label_imports.log"

#: Shown when either stream is not a real terminal.
INTERACTIVE_REFUSAL = "label-import refuses non-interactive input (run it in a terminal)."

#: Words that make a description sentence worth quoting in a digest.
_SENTENCE_HINT_RE = re.compile(
    r"\b(?:experience|years?|required|must|minimum|senior|lead|own|mentor)\w*",
    re.IGNORECASE,
)

#: Sentence-ish chunks (up to the punctuation, trailing whitespace kept out).
_SENTENCE_SPLIT_RE = re.compile(r"[^.!?\n]+[.!?]*")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_pool(path: Path | str) -> list[dict]:
    """Read a pool JSONL file, keeping only usable, displayable entries."""
    pool_path = Path(path)
    if not pool_path.exists():
        raise FileNotFoundError(f"pool file not found: {pool_path}")
    entries: list[dict] = []
    for raw in pool_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if not str(entry.get("pool_id", "")).strip():
            continue
        if not str(entry.get("url", "")).strip():
            continue
        entries.append(entry)
    return entries


def labeled_pool_ids(labels_path: Path | str) -> set[str]:
    """Pool ids already in the labels file (any parseable line counts)."""
    labels = Path(labels_path)
    if not labels.exists():
        return set()
    done: set[str] = set()
    for raw in labels.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and str(entry.get("pool_id", "")).strip():
            done.add(str(entry["pool_id"]).strip())
    return done


def ordered_pool(pool: Sequence[dict], *, seed: int = SHUFFLE_SEED) -> list[dict]:
    """Copy of ``pool`` shuffled with a fixed seed (same seed -> same order)."""
    order = list(pool)
    random.Random(seed).shuffle(order)
    return order


def pending_pool(
    pool: Sequence[dict], labels_path: Path | str, *, seed: int = SHUFFLE_SEED
) -> list[dict]:
    """Unlabeled jobs in the same fixed-seed order ``label`` walks them."""
    already = labeled_pool_ids(labels_path)
    return [e for e in ordered_pool(pool, seed=seed) if str(e["pool_id"]) not in already]


#: Location tokens that count as "open from anywhere" for ``--remote-open``.
REMOTE_OPEN_TOKENS = ("worldwide", "anywhere", "india", "apac", "asia", "remote")


def _searchable_text(entry: dict) -> str:
    """Raw pool text the filters match: location, title, first 600 chars of description."""
    location = str(entry.get("location") or "")
    title = str(entry.get("title") or "")
    description = str(entry.get("description") or "")[:600]
    return f"{location}\n{title}\n{description}".lower()


def matches_where(entry: dict, needles: Sequence[str]) -> bool:
    """True when every needle is a case-insensitive substring of the raw text."""
    if not needles:
        return True
    haystack = _searchable_text(entry)
    return all(needle.lower() in haystack for needle in needles)


def matches_exclude(entry: dict, needles: Sequence[str]) -> bool:
    """True when no needle appears in the raw text (case-insensitive)."""
    if not needles:
        return True
    haystack = _searchable_text(entry)
    return all(needle.lower() not in haystack for needle in needles)


def _locations(entry: dict) -> list[str]:
    raw = str(entry.get("location") or "")
    return [part.strip() for part in re.split(r"[/,]", raw) if part.strip()]


def matches_remote_open(entry: dict) -> bool:
    """Keep worldwide/anywhere/India/APAC/Asia/empty; drop a single named country.

    A location naming several countries is kept (unclear whether India is included);
    an empty location is kept. This is raw-text matching only, no score or filter.
    """
    parts = _locations(entry)
    if not parts:
        return True
    joined = " ".join(parts).lower()
    if any(token in joined for token in REMOTE_OPEN_TOKENS):
        return True
    return len(parts) > 1


# --------------------------------------------------------------------------- #
# Display
# --------------------------------------------------------------------------- #


def _job_frame(entry: dict, shown: int, total: int) -> str:
    def field(name: str) -> str:
        return str(entry.get(name) or "-")

    posted = str(entry.get("posted_at") or "-")
    lines = [
        "-" * 72,
        f"job {shown} of {total}",
        f"  title    : {field('title')}",
        f"  company  : {field('company')}",
        f"  location : {field('location')}",
        f"  remote   : {field('remote_type')}",
        f"  source   : {field('source')}",
        f"  posted   : {posted[:10]}",
        f"  url      : {field('url')}",
        "",
        str(entry.get("description") or "(no description)"),
        "",
    ]
    return "\n".join(lines)


def _tally_text(report: Any) -> str:
    more_valid = max(0, MIN_CASES - report.valid)
    more_traps = max(0, MIN_TRAPS - report.traps)
    more_apply = max(0, MIN_APPLY - report.applies)
    return (
        f"tally: valid {report.valid}  apply {report.applies}  "
        f"skip {report.skips}  traps {report.traps}  |  still needed: "
        f"{more_valid} valid, {more_traps} traps, {more_apply} apply "
        f"(minimums {MIN_CASES} valid / {MIN_TRAPS} traps / {MIN_APPLY} apply)"
    )


def _progress_text(pool_size: int, done: int, session: int, left: int) -> str:
    return f"progress: pool {pool_size}  labeled {done}  " f"this session {session}  left {left}"


# --------------------------------------------------------------------------- #
# File writes (only on an owner keypress or a typed "yes")
# --------------------------------------------------------------------------- #


def _append_label(labels_path: Path, entry: dict, label: str, reason: str) -> dict[str, str]:
    record = {
        "pool_id": str(entry["pool_id"]),
        "url": str(entry["url"]),
        "label": label,
        "reason": reason,
        "labeled_by": "human",
    }
    with labels_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
    return record


def _last_label(labels_path: Path) -> dict | None:
    """The last parseable record in the labels file, or ``None``."""
    if not labels_path.exists():
        return None
    last: dict | None = None
    for raw in labels_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            last = entry
    return last


def _remove_label(labels_path: Path, pool_id: str) -> int:
    """Drop every line naming ``pool_id``; returns how many were removed."""
    if not labels_path.exists():
        return 0
    kept: list[str] = []
    removed = 0
    for raw in labels_path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\r")
        try:
            entry = json.loads(line.strip())
            match = isinstance(entry, dict) and str(entry.get("pool_id", "")) == pool_id
        except json.JSONDecodeError:
            match = False
        if match:
            removed += 1
            continue
        kept.append(line)
    if removed:
        text = "\n".join(kept)
        labels_path.write_text(text + ("\n" if text else ""), encoding="utf-8", newline="\n")
    return removed


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #


def _read(read: Callable[[str], str], prompt: str, say: Callable[[str], None]) -> str | None:
    """One input line; ``None`` means EOF/interrupt, which quits the session."""
    try:
        return read(prompt)
    except (EOFError, KeyboardInterrupt):
        say("")
        say("input closed - quitting (progress is saved).")
        return None


def run_label_session(
    pool: Sequence[dict],
    labels_path: Path | str,
    *,
    seed: int = SHUFFLE_SEED,
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> int:
    """Label ``pool`` into ``labels_path`` one job per keypress.

    Returns ``0`` when the label-check minimums are met at the end and ``1``
    otherwise (the same contract as ``label-check``).
    """
    read = input_fn if input_fn is not None else input
    say = print_fn if print_fn is not None else print
    labels = Path(labels_path)
    pool_list = list(pool)
    pool_by_id = {str(e.get("pool_id", "")): e for e in pool_list}

    already = labeled_pool_ids(labels)
    queue = pending_pool(pool_list, labels, seed=seed)
    session_labeled = 0

    say(f"label: {len(pool_list)} pool jobs, {len(already)} already labeled, {len(queue)} to go")
    say(f"order: shuffled with fixed seed {seed}")
    say("keys: a = apply, s = skip, u = undo the last label, q = quit")
    say(_tally_text(validate_labels(labels, pool_by_id=pool_by_id)))

    total = len(pool_list)
    while queue:
        entry = queue.pop(0)
        shown = total - len(queue)
        say("")
        say(_job_frame(entry, shown, total))
        say(_progress_text(total, len(already), session_labeled, len(queue)))

        prompt = "label [a]pply / [s]kip / [u]ndo / [q]uit: "
        raw = _read(read, prompt, say)
        if raw is None:
            break
        key = raw.strip().lower()

        if key == "q":
            say("quit - stopping (labels already written are kept).")
            break

        if key == "u":
            last = _last_label(labels)
            if last is None:
                say("nothing to undo - the labels file is empty.")
                queue.insert(0, entry)
                continue
            undone_id = str(last.get("pool_id", ""))
            undone_entry = pool_by_id.get(undone_id)
            removed = _remove_label(labels, undone_id)
            if removed:
                say(
                    f"undid {removed} label(s) for {undone_id} "
                    f"({undone_entry.get('title') if undone_entry else 'not in this pool'})"
                )
            else:
                say(f"could not undo {undone_id} (line not found).")
            queue.insert(0, entry)
            if undone_entry is not None:
                queue.insert(0, undone_entry)
            continue

        if key not in KEY_TO_LABEL:
            say("unknown key - use a, s, u or q.")
            queue.insert(0, entry)
            continue

        label = KEY_TO_LABEL[key]
        if label == "skip":
            say("skip hints: " + " | ".join(SKIP_HINTS))
            reason_prompt = "skip reason (one line): "
        else:
            reason_prompt = "apply reason (one line): "

        reason: str | None = None
        while True:
            reason_raw = _read(read, reason_prompt, say)
            if reason_raw is None:
                break
            candidate = reason_raw.strip()
            if candidate:
                reason = candidate
                break
            say("reason must be one non-empty line.")
        if reason is None:
            break

        _append_label(labels, entry, label, reason)
        session_labeled += 1
        already.add(str(entry["pool_id"]))
        say(f"saved: {label} {entry['pool_id']} - {reason}")
        say(_progress_text(total, len(already), session_labeled, len(queue)))
        say(_tally_text(validate_labels(labels, pool_by_id=pool_by_id)))

    say("")
    say("final check (same as label-check):")
    report = validate_labels(labels, pool_by_id=pool_by_id)
    say(format_report(report))
    return 0 if report.meets_minimums else 1


# --------------------------------------------------------------------------- #
# label-sheet: the next pending jobs as compact offline text
# --------------------------------------------------------------------------- #


def short_id_of(pool_id: Any) -> str:
    """The stable short id shared by ``label-sheet`` and ``label-import``."""
    return str(pool_id)[:SHORT_ID_LEN]


@lru_cache(maxsize=1)
def _tech_terms() -> frozenset[str]:
    """Technology names from the repo ontology (>= 3 characters), lowercase."""
    try:
        ontology = get_ontology()
    except OSError:
        return frozenset()
    names = {str(name).strip().lower() for name in ontology.entries}
    return frozenset(name for name in names if len(name) >= 3)


def non_english_flag(*texts: Any) -> bool:
    """Heuristic ``[non-English?]`` flag: >=10% of letters non-ASCII (min 5)."""
    letters = 0
    non_ascii = 0
    for text in texts:
        for char in str(text or ""):
            if char.isalpha():
                letters += 1
                if ord(char) > 127:
                    non_ascii += 1
    if letters == 0 or non_ascii < NON_ENGLISH_MIN_NON_ASCII_LETTERS:
        return False
    return non_ascii / letters >= NON_ENGLISH_RATIO


def _sentence_wanted(sentence: str, tech_terms: frozenset[str]) -> bool:
    if _SENTENCE_HINT_RE.search(sentence):
        return True
    lowered = sentence.lower()
    return any(term in lowered for term in tech_terms)


def description_digest(description: Any, tech_terms: frozenset[str] | None = None) -> str:
    """First 250 characters plus up to 6 hint/technology sentences, capped at 1300.

    A sentence counts when it mentions experience/years/required/must/minimum/
    senior/lead/own/mentor (stem-matched) or a technology name from the ontology.
    The part of a sentence already covered by the 250-character prefix is not
    repeated. The result is never longer than ``DIGEST_MAX_CHARS``.
    """
    text = str(description or "")
    if tech_terms is None:
        tech_terms = _tech_terms()
    pieces: list[str] = []
    for match in _SENTENCE_SPLIT_RE.finditer(text):
        sentence = match.group(0).strip()
        if not sentence or not _sentence_wanted(sentence, tech_terms):
            continue
        start, end = match.span()
        if end <= DIGEST_PREFIX_CHARS:
            continue
        piece = sentence if start >= DIGEST_PREFIX_CHARS else text[DIGEST_PREFIX_CHARS:end].strip()
        if piece:
            pieces.append(piece)
        if len(pieces) >= DIGEST_MAX_SENTENCES:
            break
    digest = text[:DIGEST_PREFIX_CHARS]
    if pieces:
        digest = f"{digest} {' '.join(pieces)}"
    return digest[:DIGEST_MAX_CHARS]


def _field(entry: dict, name: str) -> str:
    return str(entry.get(name) or "-")


def render_sheet(entries: Sequence[dict]) -> str:
    """Compact text block list: no score, decision, filter result or ranking."""
    blocks: list[str] = []
    for entry in entries:
        description = str(entry.get("description") or "")
        lines = [
            f"short_id : {short_id_of(entry.get('pool_id', ''))}",
            f"title    : {_field(entry, 'title')}",
            f"company  : {_field(entry, 'company')}",
            f"location : {_field(entry, 'location')}",
            f"remote   : {_field(entry, 'remote_type')}",
            f"source   : {_field(entry, 'source')}",
            f"posted   : {str(entry.get('posted_at') or '-')[:10]}",
            f"url      : {_field(entry, 'url')}",
            f"digest   : {description_digest(description)}",
        ]
        if non_english_flag(entry.get("title"), description):
            lines.append("flags    : [non-English?]")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def run_label_sheet(
    pool: Sequence[dict],
    labels_path: Path | str,
    count: int,
    *,
    seed: int = SHUFFLE_SEED,
    where: Sequence[str] = (),
    exclude: Sequence[str] = (),
    remote_open: bool = False,
    sort: str = "seed",
    print_fn: Callable[[str], None] | None = None,
) -> int:
    """Print the next ``count`` unlabeled jobs in the same order ``label`` uses.

    Optional raw-text filters narrow the pending set: ``where`` (repeatable,
    case-insensitive substring match over location/title/description prefix, all
    must match), ``exclude`` (repeatable, none may match) and ``remote_open``.
    ``sort="recent"`` orders the matches by ``posted_at`` descending instead of
    the fixed seed. Only unlabeled jobs are ever shown; nothing is written.
    """
    say = print_fn if print_fn is not None else print
    if count < 1:
        say("--count must be >= 1")
        return 2
    labels = Path(labels_path)
    pending = pending_pool(pool, labels, seed=seed)
    matched = [
        entry
        for entry in pending
        if matches_where(entry, where)
        and matches_exclude(entry, exclude)
        and (not remote_open or matches_remote_open(entry))
    ]
    if sort == "recent":
        matched.sort(key=lambda entry: str(entry.get("posted_at") or ""), reverse=True)
    shown = matched[:count]
    say(
        f"label sheet: pool {len(pool)}  labeled {len(pool) - len(pending)}  "
        f"pending {len(pending)}  matched {len(matched)}  showing {len(shown)} "
        f"(fixed seed {seed})"
    )
    say("raw pool text only - nothing computed, nothing ranked")
    if not shown:
        say("no pending jobs matched.")
        return 0
    for index, entry in enumerate(shown, start=1):
        say("")
        say(f"job {index} of {len(shown)}")
        say(render_sheet([entry]))
    return 0


# --------------------------------------------------------------------------- #
# label-import: append an owner-confirmed TSV
# --------------------------------------------------------------------------- #


@dataclass
class TsvRow:
    """One data line of a label-import TSV; ``detail`` non-empty means rejected."""

    line: int
    short_id: str
    label: str
    reason: str
    detail: str = ""
    entry: dict | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.detail


def _fail(row: TsvRow, detail: str) -> None:
    if not row.detail:
        row.detail = detail


def parse_label_tsv(path: Path | str) -> tuple[list[TsvRow], str]:
    """Read the TSV; returns ``(rows, fatal_error)`` - a fatal error means unusable."""
    tsv = Path(path)
    if not tsv.exists():
        return [], f"TSV file not found: {tsv}"
    lines = tsv.read_text(encoding="utf-8-sig").splitlines()
    header_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if header_index is None:
        return [], "TSV file is empty"
    header = tuple(col.strip().lower() for col in lines[header_index].split("\t"))
    if header != TSV_COLUMNS:
        return [], "header must be exactly: " + "\t".join(TSV_COLUMNS)
    rows: list[TsvRow] = []
    for line_no, line in enumerate(lines[header_index + 1 :], start=header_index + 2):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != len(TSV_COLUMNS):
            rows.append(
                TsvRow(
                    line_no,
                    parts[0].strip() if parts else "",
                    "",
                    "",
                    detail=f"expected {len(TSV_COLUMNS)} tab-separated columns, got {len(parts)}",
                )
            )
            continue
        rows.append(TsvRow(line_no, parts[0].strip(), parts[1].strip(), parts[2].strip()))
    if not rows:
        return [], "TSV has a header but no data rows"
    return rows, ""


def _validate_rows(rows: list[TsvRow], pool: Sequence[dict], labels_path: Path) -> None:
    already = labeled_pool_ids(labels_path)
    pending = pending_pool(pool, labels_path)
    pending_ids = [str(entry["pool_id"]) for entry in pending]
    seen: set[str] = set()
    for row in rows:
        short_id = row.short_id
        if not short_id:
            _fail(row, "empty short_id")
            continue
        matches = [pid for pid in pending_ids if pid.startswith(short_id)]
        if not matches:
            if any(pid.startswith(short_id) for pid in already):
                _fail(row, "already labeled")
            else:
                _fail(row, "unknown short_id")
            continue
        if len(matches) > 1:
            _fail(row, "ambiguous short_id")
            continue
        if short_id in seen:
            _fail(row, "duplicate short_id")
            continue
        seen.add(short_id)
        if row.label.lower() not in KEY_TO_LABEL:
            _fail(row, f"label must be a or s (got {row.label!r})")
            continue
        if not row.reason.strip():
            _fail(row, "empty reason")
            continue
        row.entry = next(entry for entry in pending if str(entry["pool_id"]) == matches[0])


def is_interactive_terminal() -> bool:
    """True only when both stdin and stdout are real terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _audit_import(tsv_path: Path | str, rows: int) -> None:
    """Append timestamp, source file name, SHA-256 and row count to the audit log."""
    source = Path(tsv_path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    IMPORT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with IMPORT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{stamp}\t{source.name}\t{digest}\t{rows}\n")
        fh.flush()


def _write_rows(labels: Path, rows: list[TsvRow]) -> None:
    """Private write path: only reached after a typed ``yes`` on a real terminal."""
    for row in rows:
        if row.entry is None:
            continue
        _append_label(labels, row.entry, KEY_TO_LABEL[row.label.lower()], row.reason.strip())


def _import_with_prompt(
    pool: Sequence[dict],
    labels_path: Path | str,
    tsv_path: Path | str,
    *,
    read: Callable[[str], str],
    say: Callable[[str], None],
) -> int:
    """Validate, show the table, read the confirmation, then write and audit."""
    labels = Path(labels_path)
    rows, fatal = parse_label_tsv(tsv_path)
    if fatal:
        say(fatal)
        return 2
    _validate_rows(rows, pool, labels)

    good = sum(1 for row in rows if row.ok)
    say(f"label import: {tsv_path}")
    say(f"rows: {len(rows)}  ok: {good}  bad: {len(rows) - good}")
    say(f"{'line':>4}  {'short_id':<{SHORT_ID_LEN}}  {'label':<5}  {'status':<6}  detail")
    for row in rows:
        status = "OK" if row.ok else "BAD"
        say(
            f"{row.line:>4}  {row.short_id:<{SHORT_ID_LEN}}  {row.label or '-':<5}  "
            f"{status:<6}  {row.detail or row.reason}"
        )
    if good != len(rows):
        say("validation failed - nothing was written.")
        return 1

    prompt = f'type "yes" to append {len(rows)} label(s) to {labels}: '
    try:
        answer = read(prompt)
    except (EOFError, KeyboardInterrupt):
        say("")
        say("input closed - aborted, nothing written.")
        return 1
    if answer.strip() != "yes":
        say(f'aborted - you typed {answer.strip()!r}, not "yes"; nothing was written.')
        return 1

    _write_rows(labels, rows)
    _audit_import(tsv_path, len(rows))
    say(f"appended {len(rows)} label(s) to {labels}")
    pool_by_id = {str(entry.get("pool_id", "")): entry for entry in pool}
    report = validate_labels(labels, pool_by_id=pool_by_id)
    say(_tally_text(report))
    say(format_report(report))
    return 0 if report.meets_minimums else 1


def run_label_import(
    pool: Sequence[dict],
    labels_path: Path | str,
    tsv_path: Path | str,
) -> int:
    """Validate a TSV and append it only after a ``yes`` typed on a real terminal.

    Refuses with ``2`` (and writes nothing) unless both stdin and stdout are a
    tty; the confirmation is read from that terminal. There is no flag,
    environment variable or public function that skips the prompt - the write
    path is private to this command. Returns ``2`` for an unusable TSV, ``1``
    for rejected rows / no confirmation / unmet minimums, and ``0`` only when
    everything validated, was confirmed and the label-check minimums are met.
    """
    if not is_interactive_terminal():
        print(INTERACTIVE_REFUSAL, file=sys.stderr)
        return 2
    return _import_with_prompt(pool, labels_path, tsv_path, read=input, say=print)
