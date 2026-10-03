"""Standalone runner: ``python eval/eval.py``.

The logic lives in :mod:`atlas.evaluation`; this wrapper just makes the harness
runnable directly without going through the CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.evaluation import run_eval  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(0 if run_eval() else 1)
