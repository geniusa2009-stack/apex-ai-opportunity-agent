"""Escalation Path Between Volunteers.

See `app/models/moderation_escalation.py`'s own module docstring for
the full Step 1 investigation and Step 2 design (why this is a new,
small, polymorphic table, not five columns bolted onto each existing
claimable model).

STEP 2 DESIGN, ROUTE SHAPE: `POST/DELETE /admin/moderation/{item_type}/
{item_id}/escalate` -- deliberately the SAME `{item_type}/{item_id}`
path shape the Claim System's own `/admin/moderation/{item_type}/
{item_id}/claim` already established (`admin_claims.py`), not a
separate `/admin/escalations/{id}` resource-id-based route for
raise/resolve. Reasoning: a caller raising or resolving an escalation
is always acting FROM the context of a specific review-queue item they
are already looking at (exactly the same context `claim`/`release`
already assume) -- they know the item's type/id, not some separate
escalation row id they've never seen. `GET /admin/escalations` (the
one real resource-id-based route here) is the deliberate exception:
that view's whole point is "show me every active escalation across
every item type, with no per-item context assumed" -- see that route's
own docstring below.

`_CLAIMABLE_MODELS` is imported and reused UNCHANGED from
`admin_claims.py`, not redefined here -- an escalation is only ever
raised against the same five real claimable/reviewable item kinds a
Moderator already works from the review queues for; a sixth type this
codebase doesn't have a review queue for yet would be meaningless to
"escalate" either.

STEP 2 DESIGN, TIER SPLIT -- raising vs. resolving are DELIBERATELY
gated at different tiers, unlike claim/release (both Moderator-or-
higher at the dependency layer, with release's own stricter in-body
check). This feature's entire purpose is "a Moderator who doesn't have
enough seniority/context to decide alone can flag it for someone who
does" -- gating BOTH ends at `RequireModerationAccess` would let the
SAME plain Moderator who raised the flag immediately clear it
themselves, defeating the point. `RequireSeniorModerationAccess` on
resolve (Senior-Moderator-or-higher) mirrors this codebase's own
existing "an item's own destructive/final actions sit one tier above
its everyday actions" posture (e.g. `delete_video_permanently`'s own
Senior-Moderator gate above `override_video_decision`'s plain
Moderator one) -- resolving an escalation is this feature's own
"final word," so it belongs to the tier the escalation was raised FOR
in the first place.
"""

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireSeniorModerationAccess, effective_admin_role
from app.api.v1.endpoints.admin_claims import _CLAIMABLE_MODELS
from app.models.enums import ModerationAction, ModerationTargetType
from app.models.moderation_escalation import ModerationEscalation
from app.models.user import User
from app.schemas.moderation_escalations import EscalationListOut, EscalationOut, EscalationRaiseRequest
from app.services import moderation_escalation_service, moderation_log_service

router = APIRouter(tags=["admin-escalations"])


async def _get_item_or_404(db: DbSession, item_type: ModerationTargetType, item_id: uuid.UUID) -> None:
    model = _CLAIMABLE_MODELS.get(item_type)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{item_type.value}' is not an escalatable item type.",
        )
    item = await db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{item_type.value} not found")


def _to_out(escalation: ModerationEscalation, escalated_by_name: str | None, resolved_by_name: str | None) -> EscalationOut:
    return EscalationOut(
        id=escalation.id,
        item_type=escalation.item_type,
        item_id=escalation.item_id,
        note=escalation.note,
        escalated_by=escalation.escalated_by,
        escalated_by_name=escalated_by_name,
        escalated_at=escalation.escalated_at,
        resolved_by=escalation.resolved_by,
        resolved_by_name=resolved_by_name,
        resolved_at=escalation.resolved_at,
    )


@router.post(
    "/admin/moderation/{item_type}/{item_id}/escalate",
    response_model=EscalationOut,
)
async def escalate_item(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    payload: EscalationRaiseRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> EscalationOut:
    """Moderator-or-higher -- raising a flag for help is exactly the
    action a plain Moderator (the tier with the LEAST context/authority)
    must be able to take; see this module's own docstring for why
    resolving is gated stricter than this.

    Idempotent (200) if the item already has an active escalation --
    see `moderation_escalation_service.raise_escalation`'s own
    docstring for why a second raise doesn't stack a duplicate row.
    """
    await _get_item_or_404(db, item_type, item_id)

    escalation, created = await moderation_escalation_service.raise_escalation(
        db, item_type=item_type, item_id=item_id, note=payload.note, caller_id=current_user.id
    )

    if created:
        moderation_log_service.log_action(
            db,
            actor_id=current_user.id,
            actor_role=effective_admin_role(current_user),
            action=ModerationAction.ESCALATE,
            target_type=item_type,
            target_id=item_id,
            outcome={"note": payload.note},
        )

    await db.commit()
    await db.refresh(escalation)

    escalated_by_name = current_user.full_name if escalation.escalated_by == current_user.id else None
    return _to_out(escalation, escalated_by_name, None)


@router.delete(
    "/admin/moderation/{item_type}/{item_id}/escalate",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def resolve_item_escalation(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> None:
    """Senior-Moderator-or-higher only -- see this module's own
    docstring for the full tier-split reasoning.

    Idempotent (204) if the item has no active escalation -- same
    "nothing to be unauthorized to release" posture
    `admin_claims.release_claim` already established for an
    already-unclaimed item.
    """
    await _get_item_or_404(db, item_type, item_id)

    escalation = await moderation_escalation_service.get_active_escalation(db, item_type, item_id)
    if escalation is None:
        return

    moderation_escalation_service.resolve_escalation(
        escalation, resolver_id=current_user.id, now=dt.datetime.now(dt.timezone.utc)
    )
    db.add(escalation)

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.ESCALATION_RESOLVE,
        target_type=item_type,
        target_id=item_id,
        outcome={"escalation_id": str(escalation.id)},
    )

    await db.commit()


@router.get("/admin/escalations", response_model=EscalationListOut)
async def list_active_escalations(
    db: DbSession,
    current_user: CurrentUser,
    _: RequireSeniorModerationAccess,
) -> EscalationListOut:
    """Senior-Moderator-or-higher -- the one real resource-id-based
    route in this file (see module docstring): every currently ACTIVE
    escalation (`resolved_at IS NULL`), across every item type, oldest
    first (same "the longest-waiting item surfaces first" posture
    `get_opportunity_review_queue`/`get_review_queue` already
    established) -- exactly the view this feature exists to give a
    Senior Moderator/Owner: "what has been flagged for me, across every
    queue, right now."
    """
    filters = (ModerationEscalation.resolved_at.is_(None),)

    total = (
        await db.execute(select(func.count()).select_from(ModerationEscalation).where(*filters))
    ).scalar_one()

    Escalator = aliased(User)
    result = await db.execute(
        select(ModerationEscalation, Escalator.full_name)
        .outerjoin(Escalator, Escalator.id == ModerationEscalation.escalated_by)
        .where(*filters)
        .order_by(ModerationEscalation.escalated_at.asc())
    )
    rows = result.all()

    items = [_to_out(escalation, escalated_by_name, None) for escalation, escalated_by_name in rows]

    return EscalationListOut(items=items, total=total)
