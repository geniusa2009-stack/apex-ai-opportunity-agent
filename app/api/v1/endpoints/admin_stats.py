"""Phase 5 (Contribution Stats + The 3 Views, backend-only) -- two new
Owner-only aggregation endpoints. Builds on the confirmed-real activity
log (commit 5d2934e) and claim system (commit 47906f0) -- does NOT
rebuild `GET /admin/activity-log/{user_id}` (Phase 3's own per-volunteer
timeline view already covers that; see this repo's own Phase 5 prompt).

    GET /admin/team/summary?period=week|month  -- per-volunteer real
                                                   counts + response-time
                                                   metrics, this step's
                                                   own "weekly/monthly
                                                   team summary" view
    GET /admin/today                           -- single real payload,
                                                   this step's own "daily
                                                   what happened today"
                                                   digest view

Both Owner-only (`RequireOwner`), same posture as
`admin_roles.py`/`admin_activity_log.py` -- this step's own ask names
both as Owner-only explicitly.

Routing-precedence check (same discipline as every other router in this
codebase): `/admin/team/summary` diverges from the EXISTING
`GET /admin/team` (`admin_roles.py`) at the THIRD path segment
(`/admin/team` has no further segment at all; `/admin/team/summary` adds
one literal `summary` segment) -- these are two distinct, non-ambiguous
path templates regardless of registration order; FastAPI/Starlette only
worries about ambiguity when one template's OWN segments could swallow
another's (e.g. an untyped `{param}` sharing a position with a literal),
which isn't the case here since `/admin/team` simply has no third
segment to collide with. `/admin/today` is a brand-new top-level literal
under `admin/` that doesn't share ANY segment position with `admin/
videos/*`, `admin/users/*`, `admin/team`, `admin/activity-log*`, or
`admin/moderation/*` -- no ambiguity with any other router already
registered.

See `app/services/moderation_stats_service.py`'s own module docstring
for the full Step 1 response-time-definition reasoning and the two real
aggregation queries these two routes call.
"""

import datetime as dt
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, RequireOwner
from app.schemas.moderation_stats import TeamSummaryResponse, TodayDigestResponse, VolunteerPeriodSummary
from app.services import moderation_stats_service

router = APIRouter(tags=["admin-stats"])


@router.get("/admin/team/summary", response_model=TeamSummaryResponse)
async def get_team_summary(
    db: DbSession,
    _: RequireOwner,
    period: Literal["week", "month"] = Query(default="week"),
) -> TeamSummaryResponse:
    """Owner-only. Per-volunteer real counts by `ModerationAction` type
    plus real response-time metrics, over the real, server-computed
    current `week`/`month` window -- see
    `moderation_stats_service.period_bounds`'s own docstring for the
    real boundary definitions (calendar-aligned, UTC) and why this
    endpoint computes them server-side rather than accepting client
    `start`/`end` the way `GET /admin/activity-log` does.

    Only volunteers with at least one real logged action in the window
    appear in `items` -- this is an activity summary, not a roster; `GET
    /admin/team` (`admin_roles.py`) already serves the full roster,
    including anyone with zero activity.
    """
    try:
        start, end = moderation_stats_service.period_bounds(period)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    rows = await moderation_stats_service.get_team_summary(db, start=start, end=end)
    items = [
        VolunteerPeriodSummary(
            actor_id=row.actor_id,
            actor_email=row.actor_email,
            actor_full_name=row.actor_full_name,
            action_counts=row.action_counts,
            total_actions=row.total_actions,
            avg_response_time_seconds=row.avg_response_time_seconds,
            response_time_sample_count=row.response_time_sample_count,
        )
        for row in rows
    ]
    return TeamSummaryResponse(period=period, start=start, end=end, items=items)


@router.get("/admin/today", response_model=TodayDigestResponse)
async def get_today_digest(db: DbSession, _: RequireOwner) -> TodayDigestResponse:
    """Owner-only. Step 3's single real daily-digest payload -- see
    `moderation_stats_service.get_today_digest`'s own docstring for the
    real UTC-calendar-day window, the two real aggregate queries backing
    `total_actions`/`active_volunteer_count`, and why
    `longest_unclaimed_wait_seconds` is a LIVE snapshot against the
    review queue's current state rather than a historical aggregate.
    """
    digest = await moderation_stats_service.get_today_digest(db)
    return TodayDigestResponse(
        date=digest.date,
        window_start=digest.window_start,
        window_end=digest.window_end,
        total_actions=digest.total_actions,
        active_volunteer_count=digest.active_volunteer_count,
        longest_unclaimed_wait_seconds=digest.longest_unclaimed_wait_seconds,
        longest_unclaimed_item_id=digest.longest_unclaimed_item_id,
    )
