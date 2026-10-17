"""Small shared text helpers (no project imports, safe for any layer)."""

from __future__ import annotations

import re

#: A leading list conjunction some extraction prompts leave on a list item
#: ("and Vercel." -> "Vercel").
_LEADING_AND_RE = re.compile(r"^\s*and\s+", re.IGNORECASE)

#: A trailing period (and any whitespace around it) to drop from a list item.
_TRAILING_PERIOD_RE = re.compile(r"[\s.]*\.\s*$")


def _clean_tech_item(item: str) -> str:
    value = _LEADING_AND_RE.sub("", item or "").strip()
    value = _TRAILING_PERIOD_RE.sub("", value).strip()
    return value


def clean_tech_items(items: list[str]) -> list[str]:
    """Normalise a tech list: strip a leading "and " and trailing periods/space, dedupe.

    The owner's extraction sometimes emits entries like ``"and Vercel."``; this turns
    them into plain, deduplicated names so they can be cited as evidence and shown as
    skills. Order is preserved (first occurrence wins).
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = _clean_tech_item(item)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out
