"""Feedback capture and light, bounded tuning (plan section 5.12 / Phase 7).

Feedback (good / bad + reason code) is stored per job and then folded back into
the settings that drive future runs:

* ``bad`` + ``bad_company``      -> company blacklisted (hard filter).
* ``good``                       -> company added to target_companies (rank bonus).
* ``bad`` + ``too_senior``       -> seniority_fit weight nudged up.
* ``bad`` + ``wrong_stack``      -> must_have_coverage weight nudged up.
* ``good``                       -> fit_score weight nudged up.

Weights are clamped then renormalised to sum to 1, so tuning is always bounded
and cannot run away. ``export_golden`` grows the labelled eval set from feedback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import REPO_ROOT, Settings
from .db import load_feedback

REASON_CODES = ["too_senior", "wrong_stack", "bad_company", "wrong_location", "other", "good_fit"]
WEIGHT_KEYS = ("fit_score", "must_have_coverage", "seniority_fit", "preference_bonus")
NUDGE = 0.02
NUDGE_GOOD = 0.01
FEEDBACK_GOLDEN_PATH = REPO_ROOT / "eval" / "feedback.jsonl"


@dataclass
class FeedbackTuning:
    weights: dict[str, float] = field(default_factory=dict)
    blacklist_added: list[str] = field(default_factory=list)
    targets_added: list[str] = field(default_factory=list)
    feedback_count: int = 0

    def summary(self) -> str:
        if self.feedback_count == 0:
            return "No feedback recorded yet."
        bits = [f"{self.feedback_count} feedback item(s) applied"]
        if self.blacklist_added:
            bits.append(f"blacklisted: {', '.join(self.blacklist_added)}")
        if self.targets_added:
            bits.append(f"boosted: {', '.join(self.targets_added)}")
        return "; ".join(bits)


def record_feedback(
    conn,
    job_id: int,
    verdict: str,
    *,
    reason_code: str | None = None,
    note: str | None = None,
) -> int:
    from .db import record_feedback as _record

    if verdict not in {"good", "bad"}:
        raise ValueError("verdict must be 'good' or 'bad'")
    if reason_code is not None and reason_code not in REASON_CODES:
        raise ValueError(f"reason_code must be one of {REASON_CODES}")
    return _record(conn, job_id, verdict, reason_code, note)


def _renormalise(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return weights
    return {key: round(value / total, 6) for key, value in weights.items()}


def apply_feedback(settings: Settings, feedback_rows: list[dict]) -> tuple[Settings, FeedbackTuning]:
    tuned = settings.model_copy(deep=True)
    tuning = FeedbackTuning(feedback_count=len(feedback_rows))
    if not feedback_rows:
        return tuned, tuning

    weights = {key: getattr(tuned.ranking.weights, key) for key in WEIGHT_KEYS}
    blacklist = {c.lower(): c for c in tuned.companies.blacklist_companies}
    targets = {c.lower(): c for c in tuned.companies.target_companies}

    for row in feedback_rows:
        company = (row.get("company") or "").strip()
        company_key = company.lower()
        if row["verdict"] == "good":
            weights["fit_score"] += NUDGE_GOOD
            if company and company_key not in targets:
                targets[company_key] = company
                tuning.targets_added.append(company)
        else:
            reason = row.get("reason_code")
            if reason == "bad_company" and company and company_key not in blacklist:
                blacklist[company_key] = company
                tuning.blacklist_added.append(company)
            elif reason == "too_senior":
                weights["seniority_fit"] += NUDGE
            elif reason == "wrong_stack":
                weights["must_have_coverage"] += NUDGE

    weights = {key: max(0.0, min(1.0, value)) for key, value in weights.items()}
    weights = _renormalise(weights)

    for key, value in weights.items():
        setattr(tuned.ranking.weights, key, value)
    tuned.companies.blacklist_companies = sorted(blacklist.values())
    tuned.companies.target_companies = sorted(targets.values())
    tuning.weights = weights
    return tuned, tuning


def load_and_apply(conn, settings: Settings) -> tuple[Settings, FeedbackTuning]:
    return apply_feedback(settings, load_feedback(conn))


def export_golden(conn, path: Path | str = FEEDBACK_GOLDEN_PATH) -> int:
    """Append labelled feedback jobs to a JSONL the eval harness can read."""
    rows = load_feedback(conn)
    written = 0
    path = Path(path)
    existing_ids = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing_ids.add(json.loads(line)["id"])
            except (ValueError, KeyError):
                continue

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            if not row.get("description_raw") or not row.get("title"):
                continue
            case_id = f"feedback_{row['job_id']}"
            if case_id in existing_ids:
                continue
            record = {
                "id": case_id,
                "title": row["title"],
                "description": row["description_raw"],
                "label": "apply" if row["verdict"] == "good" else "skip",
                "reason": f"user feedback: {row.get('reason_code') or 'n/a'}",
                "company": row.get("company"),
            }
            handle.write(json.dumps(record) + "\n")
            written += 1
    return written
