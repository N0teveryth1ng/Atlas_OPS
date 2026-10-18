"""Interactive, resumable pool labelling: ``python -m atlas.cli label``.

The human drives every keystroke. The tool shows **one** unlabeled job from the
labeling pool at a time - title, company, location, remote type, source, posted
date, URL and the full description - in a random order drawn with a fixed seed,
and appends exactly one JSON object to the labels file per accepted label.

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

import json
import random
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .labeling import MIN_APPLY, MIN_CASES, MIN_TRAPS, format_report, validate_labels

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
# File writes (keypress only)
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
    queue = [e for e in ordered_pool(pool_list, seed=seed) if str(e["pool_id"]) not in already]
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
