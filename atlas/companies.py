"""Target-company ATS board list (``companies.yaml``)."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import REPO_ROOT

COMPANIES_PATH = REPO_ROOT / "companies.yaml"


def load_companies(path: Path | str = COMPANIES_PATH) -> dict[str, list[str]]:
    p = Path(path)
    if not p.exists():
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {
        source: [str(token) for token in (tokens or [])]
        for source, tokens in (raw.get("companies") or {}).items()
    }
