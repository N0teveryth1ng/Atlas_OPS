"""Structured logging setup.

Call :func:`setup_logging` once at process start (CLI entrypoint or scheduled
run). Logs go to stderr and to ``logs/atlas.log``.

A per-run ``run_id`` can be attached via :func:`set_run_id` so every record
from a single pipeline execution is greppable.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import REPO_ROOT

LOG_DIR = REPO_ROOT / "logs"
DEFAULT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

_run_id: str = "-"


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        record.run_id = _run_id
        return True


def set_run_id(run_id: str) -> None:
    global _run_id
    _run_id = run_id


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root logging (idempotent)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    if any(getattr(h, "_atlas_handler", False) for h in root.handlers):
        return root

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | run=%(run_id)s | %(name)s | %(message)s"
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(_RunIdFilter())
    stream._atlas_handler = True  # type: ignore[attr-defined]
    root.addHandler(stream)

    file_handler = logging.FileHandler(LOG_DIR / "atlas.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_RunIdFilter())
    file_handler._atlas_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    return root
