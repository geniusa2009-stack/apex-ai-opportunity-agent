"""Review of Suspicious New Signups -- `GET /admin/signups/review`, a
real, Owner-pollable list of recently created accounts annotated with
real, independently-explainable signals. See
`app/services/signup_review_service.py`'s own module docstring for the
full Step 1 investigation this endpoint is built on (what real signals
this system's Supabase-only auth architecture actually supports, and
what was deliberately NOT built because there is no real correlation
data to justify it).

TIER DECISION, DISCLOSED (this task's own required Step 3.2 reasoning):
`RequireOwner`, matching `GET /admin/flagged-users` and
`GET /admin/high-risk` exactly -- same platform-wide, cross-cutting
aggregate-view class those two already established (as opposed to a
scoped, per-case Moderator read), so this follows the same precedent
rather than introducing a third, inconsistent answer to the same kind
of tier question.

PULL-BASED, NO PUSH, DISCLOSED: a plain `GET` the Owner calls when they
choose to look, exactly like the two views above -- no notification, no
badge, no unread count anywhere.
"""

import datetime as dt

from fastapi import APIRouter, Query

from app.api.deps import DbSession, RequireOwner
from app.schemas.signup_review import RecentSignupListResponse, RecentSignupOut
from app.services import signup_review_service

router = APIRouter(tags=["admin-signups"])


def _build_reasons(cohort_size: int, blacklist_trigger_count: int, moderation_action_count: int) -> list[str]:
    """Real, plain-English strings built directly from the three real
    underlying facts -- never an opaque score. Order matches
    `get_recent_signups`'s own sort priority (velocity first, then
    blacklist, then moderation actions)."""
    reasons: list[str] = []
    if cohort_size > 0:
        window = signup_review_service.SIGNUP_VELOCITY_WINDOW_MINUTES
        reasons.append(
            f"{cohort_size} other account(s) created within {window} minutes of this one"
        )
    if blacklist_trigger_count > 0:
        reasons.append(
            f"Blacklisted content rejected {blacklist_trigger_count} time(s) since joining"
        )
    if moderation_action_count > 0:
        reasons.append(
            f"{moderation_action_count} real moderation action(s) taken against this account since joining"
        )
    return reasons


@router.get("/admin/signups/review", response_model=RecentSignupListResponse)
async def get_recent_signups(
    db: DbSession,
    _: RequireOwner,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RecentSignupListResponse:
    """Owner-only (see module docstring). Real recently-created accounts,
    each annotated with real, independently-explainable signals -- no
    unified "risk score" anywhere in this handler or the service it
    calls.
    """
    candidates, total = await signup_review_service.get_recent_signups(db, limit=limit, offset=offset)

    items = [
        RecentSignupOut(
            user_id=c.user_id,
            full_name=c.full_name,
            email=c.email,
            created_at=c.created_at,
            cohort_size=c.cohort_size,
            blacklist_trigger_count=c.blacklist_trigger_count,
            moderation_action_count=c.moderation_action_count,
            reasons=_build_reasons(c.cohort_size, c.blacklist_trigger_count, c.moderation_action_count),
        )
        for c in candidates
    ]

    return RecentSignupListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        window_days=signup_review_service.RECENT_SIGNUP_LOOKBACK_DAYS,
        velocity_window_minutes=signup_review_service.SIGNUP_VELOCITY_WINDOW_MINUTES,
        generated_at=dt.datetime.now(dt.timezone.utc),
    )
