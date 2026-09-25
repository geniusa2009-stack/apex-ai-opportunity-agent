"""Application Intelligence step, Part B/U: a deterministic
`TrackerStatus` transition table.

AUDITED FIRST (Part 0): the pre-existing `PATCH /tracker/{id}/status`
route (`app/api/v1/endpoints/application_tracker.py`) accepted ANY
`TrackerStatus` value with zero transition validation -- a real,
pre-existing gap this phase's own Part U/8 explicitly targets ("A
request such as PATCH status=SUBMITTED must not bypass the submission
pathway"). Rather than invent a brand-new lifecycle vocabulary (Part B's
own "do not blindly create all states if existing project terminology
differs" -- this project already has `TrackerStatus`, shipped, in use),
this module adds a transition table on top of the EXISTING enum, mirrors
`opportunity_state_machine.py`'s own established `_ALLOWED_TRANSITIONS`
pattern, and is the one new gate `application_tracker_service.
update_tracker_status` now runs every transition through.

`APPLIED` is deliberately UNREACHABLE from this table's own
`is_valid_transition` for the generic status-update path -- see
`application_tracker_service.submit_tracker`, the one and only way to
reach `APPLIED`, which also stamps `ApplicationTracker.submitted_at`
(Part M). This is enforced here, not just documented, by simply never
listing `APPLIED` as a reachable target for `validate_generic_transition`.
"""

from __future__ import annotations

from app.models.enums import TrackerStatus

# Reachable via the GENERIC status-update path only -- APPLIED is
# excluded everywhere on purpose (see module docstring).
_ALLOWED_GENERIC_TRANSITIONS: dict[TrackerStatus, frozenset[TrackerStatus]] = {
    TrackerStatus.INTERESTED: frozenset({
        TrackerStatus.PREPARING_DOCS, TrackerStatus.WITHDRAWN,
    }),
    TrackerStatus.PREPARING_DOCS: frozenset({
        TrackerStatus.INTERESTED, TrackerStatus.WITHDRAWN,
    }),
    # A real applicant can be called for/complete an interview stage
    # either before or after Gemini's own "applied" milestone concept --
    # this table only governs the TRACKER's own status, not the real
    # provider's process, so both directions are legitimate depending on
    # how a given program actually runs interviews.
    TrackerStatus.INTERVIEWING: frozenset({
        TrackerStatus.WITHDRAWN,
    }),
    TrackerStatus.APPLIED: frozenset({
        TrackerStatus.INTERVIEWING, TrackerStatus.ACCEPTED, TrackerStatus.REJECTED,
        TrackerStatus.WITHDRAWN,
    }),
    TrackerStatus.ACCEPTED: frozenset(),  # terminal
    TrackerStatus.REJECTED: frozenset(),  # terminal
    TrackerStatus.WITHDRAWN: frozenset(),  # terminal -- a real, final user decision
}

# The ONLY transitions allowed to reach APPLIED -- checked exclusively
# by `submit_tracker`'s own dedicated call to `validate_submission_transition`.
_SUBMITTABLE_FROM: frozenset[TrackerStatus] = frozenset({
    TrackerStatus.INTERESTED, TrackerStatus.PREPARING_DOCS, TrackerStatus.INTERVIEWING,
})


class InvalidTransitionError(Exception):
    """Raised when a proposed `TrackerStatus` transition is not allowed
    from the tracker's current status."""

    def __init__(self, current: str, proposed: str, *, reason: str = ""):
        self.current = current
        self.proposed = proposed
        self.reason = reason
        super().__init__(f"Cannot transition from {current!r} to {proposed!r}: {reason}")


def validate_generic_transition(current: str, proposed: str) -> None:
    """Raises `InvalidTransitionError` unless `proposed` is a real,
    allowed transition from `current` via the generic status-update
    path. Idempotent no-op (current == proposed) is always valid."""
    if current == proposed:
        return
    try:
        current_enum = TrackerStatus(current)
        proposed_enum = TrackerStatus(proposed)
    except ValueError as exc:
        raise InvalidTransitionError(current, proposed, reason="unknown status value") from exc

    if proposed_enum == TrackerStatus.APPLIED:
        raise InvalidTransitionError(
            current, proposed,
            reason="submission must go through the dedicated submit endpoint, not a generic status update",
        )

    allowed = _ALLOWED_GENERIC_TRANSITIONS.get(current_enum, frozenset())
    if proposed_enum not in allowed:
        raise InvalidTransitionError(current, proposed, reason="not a valid transition")


def validate_submission_transition(current: str) -> None:
    """Raises `InvalidTransitionError` unless `current` is a status the
    dedicated submission pathway may move to APPLIED from."""
    try:
        current_enum = TrackerStatus(current)
    except ValueError as exc:
        raise InvalidTransitionError(current, TrackerStatus.APPLIED.value, reason="unknown status value") from exc

    if current_enum == TrackerStatus.APPLIED:
        raise InvalidTransitionError(
            current, TrackerStatus.APPLIED.value, reason="this application was already submitted",
        )
    if current_enum not in _SUBMITTABLE_FROM:
        raise InvalidTransitionError(
            current, TrackerStatus.APPLIED.value, reason="cannot submit from this status",
        )
