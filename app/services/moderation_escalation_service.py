"""Escalation Path Between Volunteers -- service layer.

Owns the real write paths (`raise_escalation`/`resolve_escalation`) and
the "is this item currently escalated" read helper, same 3-layer
convention (Models/Schemas -> Service -> Router) every other
moderation feature in this codebase already follows.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ModerationTargetType
from app.models.moderation_escalation import ModerationEscalation


async def get_active_escalation(
    db: AsyncSession, item_type: ModerationTargetType, item_id: uuid.UUID
) -> ModerationEscalation | None:
    """The one real place "is this item currently escalated" gets
    decided -- both `raise_escalation`'s own idempotency check and any
    future per-item-detail view should call THIS rather than querying
    `moderation_escalations` directly, same "one seam, not one query
    per call site" posture `moderation_claim_service.get_effective_claim`
    already established for claims."""
    result = await db.execute(
        select(ModerationEscalation)
        .where(
            ModerationEscalation.item_type == item_type,
            ModerationEscalation.item_id == item_id,
            ModerationEscalation.resolved_at.is_(None),
        )
        .order_by(ModerationEscalation.escalated_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def raise_escalation(
    db: AsyncSession,
    *,
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    note: str,
    caller_id: uuid.UUID,
) -> tuple[ModerationEscalation, bool]:
    """Returns `(escalation, created)`. Idempotent, same posture as
    `moderation_claim_service.apply_claim`'s own "re-calling isn't an
    error" design: if the item already has an active escalation, that
    SAME row is returned unchanged (`created=False`) rather than
    stacking a second, redundant one -- two Moderators independently
    noticing the same case needs senior attention is a real, expected
    scenario, not a conflict to reject. The existing escalation's own
    `note`/`escalated_by` are deliberately left untouched (the FIRST
    reason raised is the one shown to whoever resolves it; a second
    Moderator wanting to add context can still do so via this item's
    own comment/notes surface once Deliverable 4 -- Internal Notes on
    Cases -- exists, not by silently overwriting this row).
    """
    existing = await get_active_escalation(db, item_type, item_id)
    if existing is not None:
        return existing, False

    escalation = ModerationEscalation(
        item_type=item_type,
        item_id=item_id,
        note=note,
        escalated_by=caller_id,
    )
    db.add(escalation)
    await db.flush()
    return escalation, True


def resolve_escalation(escalation: ModerationEscalation, *, resolver_id: uuid.UUID, now: dt.datetime) -> None:
    """Mutates `escalation.resolved_by`/`.resolved_at` in place (caller
    adds it to their own session/commits) -- same "service returns a
    mutated object, doesn't manage its own transaction" posture as
    `moderation_claim_service.apply_release`. `now` is passed in
    (`datetime.now(timezone.utc)`) rather than computed here, so tests
    can assert on an exact, known timestamp without monkeypatching a
    clock function -- same reasoning `moderation_claim_service._now`'s
    own docstring gives for its different (module-seam) approach; this
    function is simpler still since resolution has no expiry logic that
    needs a single, consistent "now" reused across a whole call, so a
    plain parameter is enough.
    """
    escalation.resolved_by = resolver_id
    escalation.resolved_at = now
