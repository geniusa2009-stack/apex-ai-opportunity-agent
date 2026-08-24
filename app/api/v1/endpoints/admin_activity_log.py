"""Automatic Moderation Activity Log step -- Owner-only read endpoints.

Two endpoints, both `RequireOwner`-gated, per this step's own explicit
"Owner-only for now, per the 'only I see it' decision":

    GET /admin/activity-log/{user_id}  -- one volunteer's real timeline
    GET /admin/activity-log            -- the whole team's log, filterable
                                           by date range

Routing-precedence check (same discipline as every other router in this
codebase): this router's two literal paths (`admin/activity-log/{user_id}`,
`admin/activity-log`) diverge at the SECOND path segment (`activity-log`)
from every other `admin/*` route already registered
(`admin/videos/*`, `admin/courses/*`, `admin/users/*`, `admin/team`) --
no FastAPI/Starlette path-template ambiguity, registration order is
provably irrelevant. The two routes here also don't collide with EACH
OTHER: `admin/activity-log` (no further segment) and
`admin/activity-log/{user_id}` (one more literal segment) are distinct
path templates regardless of declaration order.
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Query

from app.api.deps import DbSession, RequireOwner
from app.schemas.moderation_log import ModerationLogEntryOut, ModerationLogListResponse
from app.services import moderation_log_service
from app.services.moderation_log_service import LogEntryWithActor

router = APIRouter(tags=["admin-activity-log"])


def _to_response(rows: list[LogEntryWithActor], *, total: int, limit: int, offset: int) -> ModerationLogListResponse:
    items = [
        ModerationLogEntryOut(
            id=entry.id,
            actor_id=entry.actor_id,
            actor_email=email,
            actor_full_name=full_name,
            actor_role=entry.actor_role,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            outcome=entry.outcome,
            created_at=entry.created_at,
        )
        for entry, email, full_name in rows
    ]
    return ModerationLogListResponse(items=items, total=total, limit=limit, offset=offset)


@router.get("/admin/activity-log/{user_id}", response_model=ModerationLogListResponse)
async def get_actor_activity_log(
    user_id: uuid.UUID,
    db: DbSession,
    _: RequireOwner,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ModerationLogListResponse:
    """Owner-only. One specific volunteer's real moderation timeline,
    newest first. Does NOT 404 for a `user_id` that doesn't exist or has
    no log entries -- an empty `items`/`total=0` list is the honest,
    correct answer for "this person has never taken a logged action" and
    for "this id doesn't correspond to a real user" alike; there is no
    real distinction Phase 5's views would need drawn between those two
    cases (both render the same "nothing to show" empty state), so no
    extra `User` existence lookup is spent proving one over the other.
    """
    rows, total = await moderation_log_service.list_actor_log(db, user_id, limit=limit, offset=offset)
    return _to_response(rows, total=total, limit=limit, offset=offset)


@router.get("/admin/activity-log", response_model=ModerationLogListResponse)
async def get_team_activity_log(
    db: DbSession,
    _: RequireOwner,
    start: dt.datetime | None = Query(default=None, description="Inclusive lower bound on created_at."),
    end: dt.datetime | None = Query(default=None, description="Exclusive upper bound on created_at."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ModerationLogListResponse:
    """Owner-only. The whole team's real moderation log, newest first,
    optionally bounded to `[start, end)`. See
    `moderation_log_service.list_team_log`'s own docstring for the
    disclosed "plain date-range filter here, not a dedicated 'today'
    endpoint" design call this step's own ask explicitly invited.

    Phase 5's "today" view: `start=<today 00:00 local>,
    end=<tomorrow 00:00 local>`. Its "this week" view:
    `start=<most recent Monday 00:00>, end=<today 00:00> + 1 day`. Both
    are ordinary client-computed values against this one endpoint --
    no server-side "what does 'today' mean" timezone guess is baked in
    here, which would be the wrong call for a log likely read by
    volunteers across different timezones.
    """
    rows, total = await moderation_log_service.list_team_log(
        db, start=start, end=end, limit=limit, offset=offset
    )
    return _to_response(rows, total=total, limit=limit, offset=offset)
