"""Job lifecycle state machine + shipping invariants (audit D-4 / L8).

A job moves through a fixed set of states. Illegal transitions raise, and the
"a filtered job can never ship" invariant is enforced in one place so it cannot
be bypassed by calling the ranker, digest, or emailer directly.
"""

from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    new = "new"
    parsed = "parsed"
    passed_filters = "passed_filters"
    rejected = "rejected"
    needs_review = "needs_review"
    evaluated = "evaluated"
    verified = "verified"
    ranked = "ranked"
    sent = "sent"


class IllegalTransition(RuntimeError):
    """Raised when a job is asked to move between states illegally."""


class InvariantViolation(RuntimeError):
    """Raised when a filtered/needs-review job would reach a shipping stage."""


ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.new: frozenset({JobStatus.parsed}),
    JobStatus.parsed: frozenset(
        {JobStatus.passed_filters, JobStatus.rejected, JobStatus.needs_review}
    ),
    JobStatus.passed_filters: frozenset({JobStatus.evaluated, JobStatus.needs_review}),
    JobStatus.evaluated: frozenset({JobStatus.verified, JobStatus.needs_review}),
    JobStatus.verified: frozenset({JobStatus.ranked, JobStatus.needs_review}),
    JobStatus.ranked: frozenset({JobStatus.sent}),
    JobStatus.needs_review: frozenset(),
    JobStatus.rejected: frozenset(),
    JobStatus.sent: frozenset(),
}

# States from which a job is allowed to be rendered / emailed.
SHIPPABLE: frozenset[JobStatus] = frozenset({JobStatus.verified, JobStatus.ranked})


def can_transition(current: JobStatus, new: JobStatus) -> bool:
    return new in ALLOWED.get(current, frozenset())


def transition(current: JobStatus, new: JobStatus) -> JobStatus:
    """Return ``new`` if ``current -> new`` is legal, else raise."""
    if not can_transition(current, new):
        raise IllegalTransition(f"illegal job transition: {current.value} -> {new.value}")
    return new


def assert_shippable(status: JobStatus, filter_passed: bool) -> None:
    """Raise unless a job is in a shippable state with a passed filter result."""
    if status == JobStatus.needs_review:
        raise InvariantViolation("needs_review job must never be ranked/shipped")
    if status not in SHIPPABLE or not filter_passed:
        raise InvariantViolation(
            f"unshippable job reached a shipping stage: status={status.value} "
            f"filter_passed={filter_passed}"
        )


def assert_sent_subset(passed_job_ids: set[int | None], sent_job_ids: set[int | None]) -> None:
    """Enforce ``sent ⊆ passed`` (pipeline-end invariant)."""
    leaked = sent_job_ids - passed_job_ids
    if leaked:
        raise InvariantViolation(f"jobs shipped without passing filters: {sorted(map(str, leaked))}")
