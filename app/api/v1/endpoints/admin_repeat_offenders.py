"""Repeat-Offender Detection -- the Owner-only "flagged users" list.

    GET /admin/flagged-users -- real users at/above the real repeat-offender
                                 threshold within the real lookback window

TIER DECISION, DISCLOSED (this task's own required Step 3.3 reasoning):
`RequireOwner`, NOT `RequireModerationAccess`. This is deliberately a
STRICTER tier than the inline per-item `offender_recent_action_count`
field wired into `GET /admin/videos/review-queue` /
`GET /admin/chat/reports` (both `RequireModerationAccess`) --
the inline field is scoped, contextual information about the ONE case a
Moderator is already authorized to review; this endpoint is a
cross-cutting, platform-wide roster of every flagged person regardless of
whether any of them currently has an open case a given Moderator could
even see. That's the same shape of distinction `AdminRole`'s own
docstring already draws between `GET /admin/team` (Owner-only, a
platform-wide roster) and the per-case review queue (Moderator-tier) --
a platform-wide aggregate view is Owner-only even when the underlying
per-item facts it aggregates are each individually visible to a
Moderator.

Matches this task's own explicit design lean: "I check in on my own
schedule, no notifications" -- a real, pollable `GET` endpoint, not a
push/webhook/notification of any kind.
"""

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DbSession, RequireOwner
from app.models.user import User
from app.schemas.repeat_offenders import FlaggedUserOut, FlaggedUsersResponse
from app.services import repeat_offender_service

router = APIRouter(tags=["admin-repeat-offenders"])


@router.get("/admin/flagged-users", response_model=FlaggedUsersResponse)
async def get_flagged_users(db: DbSession, _: RequireOwner) -> FlaggedUsersResponse:
    """Owner-only (see module docstring). Every real user whose count of
    real, punitive moderation actions (video removals/rejections, chat
    bans -- both automatic and escalated, reported-message deletions;
    see `repeat_offender_service`'s own module docstring for the exact,
    closed set) is at or above `REPEAT_OFFENDER_THRESHOLD` within the
    last `REPEAT_OFFENDER_LOOKBACK_DAYS`, highest count first.
    """
    flagged = await repeat_offender_service.get_flagged_users(db)

    if not flagged:
        return FlaggedUsersResponse(
            items=[],
            window_days=repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS,
            threshold=repeat_offender_service.REPEAT_OFFENDER_THRESHOLD,
        )

    user_ids = [user_id for user_id, _count in flagged]
    result = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(user_ids)))
    user_rows = {row.id: (row.full_name, row.email) for row in result.all()}

    items = [
        FlaggedUserOut(
            user_id=user_id,
            full_name=user_rows.get(user_id, (None, None))[0],
            email=user_rows.get(user_id, (None, None))[1],
            recent_action_count=count,
        )
        for user_id, count in flagged
    ]

    return FlaggedUsersResponse(
        items=items,
        window_days=repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS,
        threshold=repeat_offender_service.REPEAT_OFFENDER_THRESHOLD,
    )
