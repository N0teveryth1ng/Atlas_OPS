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

# States from which a job is allowed to be rendered / shipped.
SHIPPABLE: frozenset[JobStatus] = frozenset({JobStatus.verified, JobStatus.ranked})

#: Pre-decision states a stalled job may be rewound to ``new`` from so a later
#: run can finish it. A decision-free job left in one of these states by an
#: interrupted run has no verdict to preserve, so restarting it is safe. Terminal
#: states (``rejected``/``needs_review``/``ranked``/``sent``) are deliberately
#: excluded: their outcome is already a decision.
RETRYABLE: frozenset[JobStatus] = frozenset(
    {JobStatus.parsed, JobStatus.passed_filters, JobStatus.evaluated, JobStatus.verified}
)


def reset_for_retry(status: JobStatus) -> JobStatus:
    """Return ``new`` for a stalled pre-decision job; ``new`` stays ``new``.

    The forward graph in ``ALLOWED`` stays strict. Only this explicit helper may
    rewind a resumable job, and it raises for terminal states.
    """
    if status == JobStatus.new:
        return JobStatus.new
    if status in RETRYABLE:
        return JobStatus.new
    raise IllegalTransition(f"cannot retry a job in terminal state: {status.value}")


#: States a filter re-run may revive when the approved profile changes. Their
#: verdict came from a filter decision, not from shipping, so it can be
#: recomputed. ``ranked``/``sent`` are deliberately excluded: an already-decided,
#: shipped outcome must never be reconsidered, and the remaining states are still
#: progressing through the normal run.
REFILTERABLE: frozenset[JobStatus] = frozenset({JobStatus.rejected, JobStatus.needs_review})


def reopen_for_refilter(status: JobStatus) -> JobStatus:
    """Return ``new`` for a rejected/needs-review job a filter re-run may revive.

    This is the one state change a profile change is allowed to make, and it must
    only be reached from a profile approval or the start of a run. ``ranked`` and
    ``sent`` (already decided and shipped) raise, as do ``new`` and the
    mid-pipeline states: those are either unaffected or handled by the normal run.
    Kept separate from the forward graph in ``ALLOWED`` so a shipped outcome can
    never be reopened through it.
    """
    if status in REFILTERABLE:
        return JobStatus.new
    raise IllegalTransition(f"cannot re-open a {status.value} job for re-filtering")


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
        raise InvariantViolation(
            f"jobs shipped without passing filters: {sorted(map(str, leaked))}"
        )
