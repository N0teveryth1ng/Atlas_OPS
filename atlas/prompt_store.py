"""Versioned prompt files.

Prompts live as separate files under ``prompts/`` so they can be reviewed and
diffed independently of code. Each file starts with a version marker::

    <!-- version: 1 -->

``load_prompt`` returns both the text and the version so every LLM result can
record which prompt produced it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from .config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "prompts"
_VERSION_RE = re.compile(r"<!--\s*version:\s*(\S+?)\s*-->")


class PromptNotFoundError(FileNotFoundError):
    pass


@lru_cache(maxsize=None)
def load_prompt(name: str) -> tuple[str, str]:
    """Load ``prompts/<name>.md``.

    Returns ``(text, version)``. Raises :class:`PromptNotFoundError` if missing.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise PromptNotFoundError(f"Prompt file not found: {path}")

    raw = path.read_text(encoding="utf-8")
    match = _VERSION_RE.search(raw)
    version = match.group(1) if match else "unversioned"
    text = _VERSION_RE.sub("", raw, count=1).strip()
    return text, version
