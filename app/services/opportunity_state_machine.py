"""Taxonomy Evolution + Temporal State Machine + Change History step,
Part B: a small, deterministic service that evaluates and transitions
`Opportunity.lifecycle_state`.

REUSES, NEVER REDESIGNS, D6/D7 (task's own explicit constraint): the
aggregate evidence decision this module reads comes from
`evidence_verification.evaluate_verification_readonly` (D6's own
real, unmodified, read-only reconstruction of "what would today's
decision be") -- this module adds no new evidence-evaluation rule of
its own. Deadline classification reuses
`deadline_intelligence.classify_deadline_text` (Opportunity Intelligence
Hardening step) unchanged -- no second deadline parser.

CONSTITUTION (Part G, enforced structurally, not by convention alone):
`evaluate_opportunity_state` and `_propose_lifecycle_state` take no
raw source text, no LLM output, and no user input as arguments at all
-- only already-backend-derived values (a `VerificationDecisionStatus`,
a `DeadlineState`, a parsed datetime). There is no parameter here a
prompt-injection payload embedded in a Telegram post could ever reach;
the only path from raw text to a state MUST first pass through D6's own
deterministic evidence rules and `deadline_intelligence`'s own inert
keyword classifier, neither of which executes text as instructions
(see each module's own docstring/tests).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.claim import Claim
from app.models.enums import OpportunityLifecycleState, VerificationDecisionStatus
from app.models.opportunity import Opportunity
from app.services.change_history_service import record_change_if_meaningful
from app.services.deadline_intelligence import DeadlineState, classify_deadline_text
from app.services.evidence_verification import evaluate_verification_readonly

# How many days before a deadline the OPEN -> DEADLINE_APPROACHING
# transition fires. A deliberately simple, documented threshold -- same
# "small, explicit, disclosed constant" posture as D6's own
# `STALE_EVIDENCE_MAX_AGE_DAYS`.
DEADLINE_APPROACHING_WINDOW_DAYS = 7

# ---------------------------------------------------------------------
# Deterministic transition table (Part B2). Keys are the CURRENT state;
# values are every state this module may ever propose moving TO from
# that state. Every state also implicitly allows staying at itself
# (idempotency, Part E) -- checked separately in `_is_valid_transition`,
# not listed redundantly below.
# ---------------------------------------------------------------------
_ALLOWED_TRANSITIONS: dict[OpportunityLifecycleState, frozenset[OpportunityLifecycleState]] = {
    OpportunityLifecycleState.DISCOVERED: frozenset({
        OpportunityLifecycleState.OPEN,
        OpportunityLifecycleState.HOLD,
        OpportunityLifecycleState.CONFLICT,
    }),
    OpportunityLifecycleState.OPEN: frozenset({
        OpportunityLifecycleState.DEADLINE_APPROACHING,
        OpportunityLifecycleState.EXTENDED,
        OpportunityLifecycleState.CLOSED,
        OpportunityLifecycleState.HOLD,
        OpportunityLifecycleState.CONFLICT,
    }),
    OpportunityLifecycleState.DEADLINE_APPROACHING: frozenset({
        OpportunityLifecycleState.OPEN,
        OpportunityLifecycleState.EXTENDED,
        OpportunityLifecycleState.CLOSED,
        OpportunityLifecycleState.HOLD,
        OpportunityLifecycleState.CONFLICT,
    }),
    OpportunityLifecycleState.EXTENDED: frozenset({
        OpportunityLifecycleState.OPEN,
        OpportunityLifecycleState.DEADLINE_APPROACHING,
        OpportunityLifecycleState.CLOSED,
        OpportunityLifecycleState.HOLD,
        OpportunityLifecycleState.CONFLICT,
    }),
    OpportunityLifecycleState.CLOSED: frozenset({
        OpportunityLifecycleState.REOPENED,
        OpportunityLifecycleState.HOLD,
        OpportunityLifecycleState.CONFLICT,
    }),
    OpportunityLifecycleState.REOPENED: frozenset({
        OpportunityLifecycleState.OPEN,
        OpportunityLifecycleState.DEADLINE_APPROACHING,
        OpportunityLifecycleState.CLOSED,
        OpportunityLifecycleState.HOLD,
        OpportunityLifecycleState.CONFLICT,
    }),
    OpportunityLifecycleState.HOLD: frozenset({
        OpportunityLifecycleState.OPEN,
        OpportunityLifecycleState.DEADLINE_APPROACHING,
        OpportunityLifecycleState.EXTENDED,
        OpportunityLifecycleState.CLOSED,
        OpportunityLifecycleState.CONFLICT,
    }),
    # Part B2's own literal rule: "CONFLICT -> OPEN only after
    # contradiction resolution is supported by evidence." Deliberately
    # narrower than every other row above -- a resolved conflict must
    # re-earn its way through OPEN before this module will propose any
    # more specific state for it on a later evaluation.
    OpportunityLifecycleState.CONFLICT: frozenset({
        OpportunityLifecycleState.OPEN,
    }),
}


def _is_valid_transition(current: OpportunityLifecycleState, proposed: OpportunityLifecycleState) -> bool:
    if current == proposed:
        return True  # idempotent no-op is always valid (Part E)
    return proposed in _ALLOWED_TRANSITIONS.get(current, frozenset())


@dataclasses.dataclass
class StateEvaluation:
    current_state: OpportunityLifecycleState
    proposed_state: OpportunityLifecycleState
    transition_allowed: bool
    reason: str
    supporting_evidence_ids: list
    d6_status: VerificationDecisionStatus
    deadline_state: DeadlineState
    evaluated_at: dt.datetime


def _propose_lifecycle_state(
    *,
    current_state: OpportunityLifecycleState,
    d6_status: VerificationDecisionStatus,
    deadline_state: DeadlineState,
    parsed_deadline: dt.datetime | None,
    now: dt.datetime,
    evaluated_claim_count: int,
) -> tuple[OpportunityLifecycleState, str]:
    """Pure function: derive a proposed lifecycle state from
    already-computed, backend-derived signals only (Part G). No I/O, no
    text, no LLM output -- see module docstring.
    """
    # HOLD (D6 HOLD): at least one claim has conflicting authoritative
    # evidence -- this is exactly Part B1's own CONFLICT definition
    # ("relevant authoritative sources contain unresolved contradictory
    # information"), not a generic "insufficient evidence" HOLD.
    if d6_status == VerificationDecisionStatus.HOLD:
        return OpportunityLifecycleState.CONFLICT, (
            "D6 reports conflicting authoritative evidence -- unresolved contradiction (CONFLICT)."
        )

    # Zero claims evaluated at all: nothing has been established yet --
    # DISCOVERED, not HOLD (Part B1: HOLD means evidence was evaluated
    # and found insufficient, not "never evaluated").
    if evaluated_claim_count == 0:
        return OpportunityLifecycleState.DISCOVERED, "No claims evaluated yet -- availability not yet established."

    # D6 STALE, or UNVERIFIED with claims that were evaluated but none
    # reached SUPPORTED: real evidence exists but is not sufficient for
    # a safe CURRENT state -- HOLD, never CLOSED (Part B1's own explicit
    # "do not conflate HOLD with CLOSED" rule).
    if d6_status in (VerificationDecisionStatus.STALE, VerificationDecisionStatus.UNVERIFIED):
        return OpportunityLifecycleState.HOLD, (
            f"D6 status is {d6_status.value} -- evidence insufficient/outdated for a safe current state (HOLD)."
        )

    # d6_status == VERIFIED from here on: real, current, uncontradicted
    # supporting evidence exists for at least one claim.
    if deadline_state == DeadlineState.CLOSED:
        return OpportunityLifecycleState.CLOSED, "Deadline evidence explicitly states applications are closed."

    if deadline_state == DeadlineState.EXTENDED:
        return OpportunityLifecycleState.EXTENDED, "Deadline evidence explicitly states the deadline was extended."

    # Deterministic supported-deadline-passed semantics (Part B2's own
    # "or deterministic supported deadline semantics" clause) -- a real,
    # parsed, currently-verified deadline that has already passed closes
    # the opportunity even with no explicit "closed" keyword.
    if parsed_deadline is not None:
        deadline_cmp = parsed_deadline if parsed_deadline.tzinfo else parsed_deadline.replace(tzinfo=dt.timezone.utc)
        if deadline_cmp < now:
            return OpportunityLifecycleState.CLOSED, (
                "Verified deadline has already passed (deterministic date comparison, no retrieval-time inference)."
            )
        days_remaining = (deadline_cmp - now).days
        if days_remaining <= DEADLINE_APPROACHING_WINDOW_DAYS:
            return OpportunityLifecycleState.DEADLINE_APPROACHING, (
                f"Verified deadline is within the {DEADLINE_APPROACHING_WINDOW_DAYS}-day warning window."
            )

    return OpportunityLifecycleState.OPEN, "D6 verifies current, uncontradicted supporting evidence; no closure/deadline signal."


async def evaluate_opportunity_state(
    db: AsyncSession,
    *,
    opportunity: Opportunity,
    claims: list[Claim] | None = None,
    now: dt.datetime | None = None,
) -> StateEvaluation:
    """Read-only evaluation: computes the proposed lifecycle state and
    whether transitioning to it from the opportunity's CURRENT persisted
    state is valid, WITHOUT writing anything. Mirrors D6's own
    `evaluate_verification_readonly`/`decide_verification` split -- see
    `reevaluate_opportunity_state` below for the write path.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    current_state = OpportunityLifecycleState(opportunity.lifecycle_state or OpportunityLifecycleState.DISCOVERED.value)

    decision = await evaluate_verification_readonly(db, opportunity_id=opportunity.id, now=now)

    if claims is None:
        result = await db.execute(select(Claim).where(Claim.opportunity_id == opportunity.id))
        claims = list(result.scalars().all())
    deadline_claim = next((c for c in claims if c.claim_type == "deadline"), None)
    deadline_raw_text = (deadline_claim.claim_value if deadline_claim else None) or ""
    d_state = classify_deadline_text(deadline_raw_text, parsed_deadline=opportunity.deadline)

    proposed, reason = _propose_lifecycle_state(
        current_state=current_state,
        d6_status=decision.status,
        deadline_state=d_state,
        parsed_deadline=opportunity.deadline,
        now=now,
        evaluated_claim_count=len(decision.evaluated_claims),
    )
    allowed = _is_valid_transition(current_state, proposed)

    return StateEvaluation(
        current_state=current_state,
        proposed_state=proposed if allowed else current_state,
        transition_allowed=allowed,
        reason=reason if allowed else (
            f"Proposed transition {current_state.value} -> {proposed.value} is not in the allowed "
            f"transition table; current state left unchanged. ({reason})"
        ),
        supporting_evidence_ids=decision.supporting_evidence_ids,
        d6_status=decision.status,
        deadline_state=d_state,
        evaluated_at=now,
    )


async def reevaluate_opportunity_state(
    db: AsyncSession, *, opportunity_id: uuid.UUID, now: dt.datetime | None = None
) -> StateEvaluation:
    """Part E's own explicitly-named operation. Loads the current
    opportunity + evidence, evaluates the proposed state, and -- ONLY
    when the transition is valid AND actually changes the persisted
    state -- writes `Opportunity.lifecycle_state`/`lifecycle_state_reason`/
    `lifecycle_state_evaluated_at` and records ONE `OpportunityChangeHistory`
    row for the `lifecycle_state` field (via `record_change_if_meaningful`,
    which itself no-ops when old==new -- Part C3).

    IDEMPOTENT (Part E's own requirement): running this twice in a row
    with unchanged evidence proposes the SAME state both times; the
    second call's `old_value == new_value` guard in
    `record_change_if_meaningful` means the second run writes no new
    history row and (since `current_state == proposed_state`) makes no
    further mutation -- safe to call repeatedly.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ValueError(f"No Opportunity found for id={opportunity_id!r}")

    evaluation = await evaluate_opportunity_state(db, opportunity=opportunity, now=now)

    if evaluation.transition_allowed and evaluation.proposed_state != evaluation.current_state:
        old_state_value = opportunity.lifecycle_state
        opportunity.lifecycle_state = evaluation.proposed_state.value
        opportunity.lifecycle_state_reason = evaluation.reason
        opportunity.lifecycle_state_evaluated_at = now

        await record_change_if_meaningful(
            db,
            opportunity_id=opportunity.id,
            field_name="lifecycle_state",
            old_value=old_state_value,
            new_value=evaluation.proposed_state.value,
            change_reason=evaluation.reason,
        )

    return evaluation
