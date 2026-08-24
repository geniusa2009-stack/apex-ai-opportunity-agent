"""Claim System (Prevent Duplicate Work on the Same Case).

Builds on the confirmed-real tiered role system (commit 39d1d0f) and
activity log (commit 5d2934e) -- neither re-investigated here, both used
as-is.

STEP 1, "WHAT IS A CASE" -- re-read `admin_moderation.py::get_review_queue`
in full before writing anything here. The review queue's only real item
kind is a `StudentVideo` row: `id`, `status` (one of `PENDING_AI`/
`REJECTED`/`FAILED`), plus everything else `AdminVideoReviewOut` already
surfaces. There is no existing separate "case" wrapper concept anywhere
in this codebase, and no second real queue exists to generalize over
today (chat bans are a Redis-backed TTL, not a reviewable queue item --
see `chat_security.py`; `ModerationTargetType.USER` in the activity log
refers to a chat-unban's TARGET, not something a volunteer "opens" and
works on). Given that, a claim is `claimed_by`/`claimed_at` FIELDS added
directly to `StudentVideo` (see that model's own column docstrings),
NOT a new polymorphic table -- the task's own Step 1 explicitly invites
this simpler option ("or a field addition to whatever the queue item
already is") when the real shape doesn't need more, and inventing a
parallel `Case` model here would mean either duplicating everything
`StudentVideo` already has or building a second layer of indirection
over it for zero real benefit today.

The `{item_type}` path segment is still real and enforced (not a
no-op) -- kept generic on purpose, matching the SAME polymorphic
`target_type` discriminator precedent `ModerationLogEntry` already
established, so a genuinely new claimable queue in the future is an
entry in `_CLAIMABLE_MODELS` below, not a new pair of endpoints or a
schema migration to this router. Today, exactly one value is real
(`student_video`) -- `user` is a syntactically valid `ModerationTargetType`
member (FastAPI accepts it at the path-parsing layer) but isn't in the
dispatch table below, so it correctly 404s as "not a claimable item
type," not a 422/500.

STEP 3, ENFORCEMENT DECISION (advisory-only, not a hard lock) --
claiming does NOT gate `override_video_decision`/`delete_video_permanently`/
`reject_video_and_purge_media` in `admin_moderation.py`; none of those
three routes check claim state at all. A volunteer (or an Owner) can
act on a video regardless of who currently holds its claim. Reasoning:
this step's own ask frames the goal as "other volunteers should SEE
it's claimed" (a visibility problem) and explicitly flags the real
tradeoff of full enforcement -- it could block an Owner from acting on
something urgent someone else claimed, which is a real, disclosed
downside for a team this small, where the entire point of the Owner
tier (see `AdminRole.OWNER`'s own docstring) is being able to act
without waiting on anyone else. Enforcing the lock correctly would also
require re-deciding "does an EXPIRED claim block, does an Owner's own
force-release count as taking the action for them, what happens if the
claim expires mid-request between the check and the mutation" -- real
edge cases a hard lock can't hand-wave away, for a team small enough
that the actual failure mode this whole step exists to prevent (two
people quietly duplicating the SAME piece of work) is already solved by
the visibility the review-queue listing now provides. If duplicate work
becomes a real, observed problem at a larger team size, enforcement is
a disclosed, straightforward upgrade FROM here (check `get_effective_claim`
at the top of each write action, 409 if held by someone else) -- not a
redesign.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, effective_admin_role
from app.models.enums import AdminRole, ModerationTargetType
from app.models.student_videos import StudentVideo
from app.schemas.moderation_claims import ModerationClaimOut
from app.services import moderation_claim_service

router = APIRouter(tags=["admin-claims"])

# STEP 1's own extension point (see module docstring) -- the only real
# entry today. A future claimable item type is added here, with no
# other change to this file's route bodies (both already look the
# model up generically via this table).
_CLAIMABLE_MODELS: dict[ModerationTargetType, type[StudentVideo]] = {
    ModerationTargetType.STUDENT_VIDEO: StudentVideo,
}


async def _get_claimable_item(db: DbSession, item_type: ModerationTargetType, item_id: uuid.UUID):
    model = _CLAIMABLE_MODELS.get(item_type)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{item_type.value}' is not a claimable item type.",
        )

    item = await db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{item_type.value} not found")

    return item


@router.post(
    "/admin/moderation/{item_type}/{item_id}/claim",
    response_model=ModerationClaimOut,
)
async def claim_item(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> ModerationClaimOut:
    """Moderator-or-higher (same tier as viewing the review queue itself
    -- claiming is a lightweight, fully reversible visibility aid, not a
    destructive action, see this module's own Step 3 reasoning for why
    it isn't gated any stricter).

    Real 409 (`ClaimConflictError` -> `HTTPException`) if actively
    claimed by someone else. Idempotent success (200, refreshed
    `claimed_at`) if unclaimed, expired, or already claimed by the
    SAME caller -- see `moderation_claim_service.apply_claim`'s own
    docstring for why re-claiming refreshes the timestamp rather than
    leaving it untouched.
    """
    item = await _get_claimable_item(db, item_type, item_id)

    try:
        moderation_claim_service.apply_claim(item, caller_id=current_user.id)
    except moderation_claim_service.ClaimConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already claimed by {exc.claimed_by} since {exc.claimed_at.isoformat()}.",
        )

    db.add(item)
    await db.commit()
    await db.refresh(item)

    return ModerationClaimOut(
        item_type=item_type,
        item_id=item_id,
        claimed_by=item.claimed_by,
        claimed_by_name=current_user.full_name,
        claimed_at=item.claimed_at,
    )


@router.delete(
    "/admin/moderation/{item_type}/{item_id}/claim",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def release_claim(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> None:
    """Moderator-or-higher at the dependency layer, PLUS a real, stricter
    check inside the body -- per this step's own literal "release,
    either by the claimant or by an Owner" spec:

    - Effectively unclaimed already (never claimed, expired, or the
      claimant's account was deleted): idempotent 204, no-op, no
      authorization check needed -- there is nothing to be unauthorized
      to release.
    - Actively claimed by the CALLER: releases, 204.
    - Actively claimed by SOMEONE ELSE: only an Owner (real force-release,
      per the ask) may proceed; anyone else gets a real 403. A
      Moderator/Senior-Moderator who isn't the claimant cannot release
      another volunteer's active claim -- only see it (the review-queue
      listing) and, per this module's own Step 3 decision, still act on
      the underlying video regardless.
    """
    item = await _get_claimable_item(db, item_type, item_id)

    effective_claimed_by, _effective_claimed_at = moderation_claim_service.get_effective_claim(item)

    if effective_claimed_by is not None and effective_claimed_by != current_user.id:
        if effective_admin_role(current_user).rank < AdminRole.OWNER.rank:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the claimant or an Owner may release this claim.",
            )

    moderation_claim_service.apply_release(item)
    db.add(item)
    await db.commit()
