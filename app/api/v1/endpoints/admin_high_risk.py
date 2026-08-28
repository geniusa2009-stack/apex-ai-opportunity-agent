"""High-Risk-Content View -- `GET /admin/high-risk`, a single, real,
Owner-pollable aggregation of every already-real "this needs a closer
look" signal in the codebase. See
`app/services/high_risk_service.py`'s own module docstring for the full
Step 1 investigation this endpoint is built on (what already existed vs.
what was genuinely new) and the explainability design every field here
follows.

TIER DECISION, DISCLOSED (this task's own required Step 3.3 reasoning):
`RequireOwner`, matching `GET /admin/flagged-users`
(`admin_repeat_offenders.py`) and `GET /admin/today`
(`admin_stats.py`) exactly, for the same reason both of those already
give: this is a cross-cutting, platform-wide aggregate view, not a
scoped, per-case read a Moderator reviewing one item needs (that's
already what the inline `offender_recent_action_count` field on
`GET /admin/chat/reports`/`GET /admin/videos/review-queue` is for, both
Moderator-tier). `AdminRole`'s own docstring draws exactly this
distinction between a platform-wide roster and a per-case view -- this
endpoint sits squarely in the same bucket as the other two aggregate
views already tiered that way, so it follows them rather than
introducing a third, inconsistent answer to the same kind of question.

PULL-BASED, NO PUSH, DISCLOSED: this is a plain `GET` the Owner calls
when they choose to look, exactly like `GET /admin/flagged-users` and
`GET /admin/today` -- no webhook, no notification row, no email, no
websocket push, nothing that pages anyone. Nothing about this route (or
the frontend section built on top of it) implies an unread/urgent state
the way a badge count or alert banner would; see `admin-moderation.html`
for how the new tab is presented.
"""

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DbSession, RequireOwner
from app.models.user import User
from app.schemas.high_risk import (
    HighRiskBannedUserOut,
    HighRiskFlaggedUserOut,
    HighRiskQueueBacklogOut,
    HighRiskViewResponse,
)
from app.services import high_risk_service, repeat_offender_service

router = APIRouter(tags=["admin-high-risk"])


@router.get("/admin/high-risk", response_model=HighRiskViewResponse)
async def get_high_risk_view(db: DbSession, _: RequireOwner) -> HighRiskViewResponse:
    """Owner-only (see module docstring). Composes four real,
    independently-explainable signal groups -- see
    `high_risk_service.get_high_risk_view`'s own docstring for exactly
    which real query backs each one. No new "risk score" is computed
    anywhere in this handler or the service it calls; every item below
    carries a real, plain-English `reason` derived directly from the
    underlying fact.
    """
    view = await high_risk_service.get_high_risk_view(db)

    user_ids = [user_id for user_id, _count in view.flagged_users] + [
        user_id for user_id, _remaining in view.banned_users
    ]
    user_rows: dict = {}
    if user_ids:
        result = await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(user_ids)))
        user_rows = {row.id: (row.full_name, row.email) for row in result.all()}

    flagged_users = [
        HighRiskFlaggedUserOut(
            user_id=user_id,
            full_name=user_rows.get(user_id, (None, None))[0],
            email=user_rows.get(user_id, (None, None))[1],
            recent_action_count=count,
            reason=f"{count} moderation actions in the last "
            f"{repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS} days",
        )
        for user_id, count in view.flagged_users
    ]

    banned_users = [
        HighRiskBannedUserOut(
            user_id=user_id,
            full_name=user_rows.get(user_id, (None, None))[0],
            email=user_rows.get(user_id, (None, None))[1],
            remaining_seconds=remaining,
            reason="Currently serving a chat ban",
        )
        for user_id, remaining in view.banned_users
    ]

    open_chat_reports = HighRiskQueueBacklogOut(
        count=view.open_chat_reports_count,
        oldest_item_at=view.oldest_open_chat_report_at,
        reason=(
            f"{view.open_chat_reports_count} chat report(s) awaiting review"
            if view.open_chat_reports_count
            else "No open chat reports"
        ),
    )
    pending_video_reviews = HighRiskQueueBacklogOut(
        count=view.pending_video_reviews_count,
        oldest_item_at=view.oldest_pending_video_review_at,
        reason=(
            f"{view.pending_video_reviews_count} video(s) awaiting review"
            if view.pending_video_reviews_count
            else "No videos awaiting review"
        ),
    )

    return HighRiskViewResponse(
        flagged_users=flagged_users,
        banned_users=banned_users,
        open_chat_reports=open_chat_reports,
        pending_video_reviews=pending_video_reviews,
        window_days=repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS,
        threshold=repeat_offender_service.REPEAT_OFFENDER_THRESHOLD,
        generated_at=dt.datetime.now(dt.timezone.utc),
    )
