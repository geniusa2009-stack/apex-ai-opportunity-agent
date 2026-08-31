"""Automatic Moderation Activity Log step -- Owner-only read endpoints.

Two endpoints, both `RequireOwner`-gated, per this step's own explicit
"Owner-only for now, per the 'only I see it' decision":

    GET /admin/activity-log/{user_id}  -- one volunteer's real timeline
    GET /admin/activity-log            -- the whole team's log, filterable
                                           by date range
    GET /admin/activity-log/export     -- Owner-only CSV download of the
                                           team log over a bounded range

Export Moderation Log addition -- Step 1 investigation confirmed no
export capability existed anywhere in this codebase for ANY data type
except `deadline_alerts.py::export_ics` (an unrelated .ics calendar
export) -- that endpoint's own `StreamingResponse` + `io.BytesIO` +
sanitized-`Content-Disposition`-filename pattern is the one real,
reusable export convention this codebase has, and `export_moderation_log`
below follows it rather than inventing a second one.

DISCLOSED DESIGN CALL (Step 2, CSV vs. JSON): CSV only. The two existing
read endpoints above already return the exact same rows as ordinary
JSON -- an Owner who wants the log as JSON already has it, through them.
CSV's actual incremental value is real: it is what a spreadsheet
(Excel/Google Sheets) opens directly for the record-keeping/sharing/
offline-archive use this whole feature exists for. A second "export as
JSON" format would just re-serve data already reachable, at real added
code cost, for no real new capability -- so this task builds the one
format that is a genuine, additive capability, not both for the sake of
optionality.

DISCLOSED DESIGN CALL (Step 2, Owner-tier): kept Owner-only, same tier
as both endpoints above (the "only I see it" decision from Phase 3 that
already gates the raw log view itself). An export is, if anything,
STRICTER than viewing the same rows in the admin panel -- it produces a
durable file that can leave this system entirely (shared with a
co-organizer, stored on a laptop, etc.), which is a broader exposure
than an in-app, paginated, session-bound view of the same rows. There is
no real argument for loosening tier on the copy that is harder to
control after the fact.

DISCLOSED DESIGN CALL (Step 2, limits): two real named constants below
(`MAX_EXPORT_ROWS`, `MAX_EXPORT_RANGE_DAYS`) bound both the row count and
the date range a single export can cover, so this can't become a way to
accidentally generate an unbounded file. `DEFAULT_EXPORT_RANGE_DAYS`
gives a sensible default range when the caller omits `start`/`end`
entirely (mirroring how `start`/`end` are already both optional on
`GET /admin/activity-log` above), rather than silently exporting the
entire, unbounded table history.

DISCLOSED DESIGN CALL (Step 3.3, self-logging): YES, the export itself
writes a real `ModerationLogEntry` (`ModerationAction.ACTIVITY_LOG_EXPORT`,
`target_type=USER`, `target_id=<the exporting Owner's own id>` -- see
that enum member's own docstring in `app/models/enums.py` for the full
reasoning on why `USER`/self-target was chosen over a new target-type
member). An Owner exporting the whole team's log is itself a real,
meaningful, auditable event -- the same reasoning `CHAT_AUTO_BAN` was
added for (closing a gap where a real action left no durable trace)
applies here, just for a read/export rather than a write.

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

import csv
import datetime as dt
import io
import json
import re
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, DbSession, RequireOwner, effective_admin_role
from app.models.enums import ModerationAction, ModerationTargetType
from app.schemas.moderation_log import ModerationLogEntryOut, ModerationLogListResponse
from app.services import moderation_log_service
from app.services.moderation_log_service import LogEntryWithActor

router = APIRouter(tags=["admin-activity-log"])

# Step 2 named constants (see this module's own docstring for the full
# reasoning behind each).
MAX_EXPORT_ROWS = 10_000
MAX_EXPORT_RANGE_DAYS = 366
DEFAULT_EXPORT_RANGE_DAYS = 90


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


def _csv_cell_target(target_type: ModerationTargetType, target_id: uuid.UUID) -> tuple[str, str]:
    """Step 1.3/Step 2 flattening decision: `target_type`/`target_id`
    become TWO separate CSV columns, not one combined string. A combined
    `"opportunity:ab12cd34"`-style cell reads fine but is worse for the
    actual spreadsheet use this export exists for -- an Owner filtering/
    sorting by target type in Excel/Sheets needs it as its own column,
    and the full (not truncated) UUID is kept so a row can be traced
    back to the real underlying record if ever needed."""
    return target_type.value, str(target_id)


@router.get("/admin/activity-log/export")
async def export_moderation_log(
    current_user: CurrentUser,
    db: DbSession,
    _: RequireOwner,
    format: Literal["csv"] = Query(default="csv", description="Only CSV is offered -- see this module's own docstring for why."),
    start: dt.datetime | None = Query(default=None, description="Inclusive lower bound on created_at. Defaults to `end` minus DEFAULT_EXPORT_RANGE_DAYS."),
    end: dt.datetime | None = Query(default=None, description="Exclusive upper bound on created_at. Defaults to now."),
) -> StreamingResponse:
    """Owner-only real CSV download of the team's moderation log over a
    bounded `[start, end)` range. Reuses `moderation_log_service.
    list_team_log` (the SAME query `GET /admin/activity-log` above already
    uses) rather than duplicating its filtering logic -- this endpoint
    only adds the CSV rendering, the default-range/max-range bounds, and
    its own self-logging on top.

    See this module's own docstring for the full Step 2 reasoning behind
    every design call made here (CSV-only, Owner-tier, the two limit
    constants, self-logging).
    """
    resolved_end = end or dt.datetime.now(dt.timezone.utc)
    resolved_start = start or (resolved_end - dt.timedelta(days=DEFAULT_EXPORT_RANGE_DAYS))

    if resolved_end <= resolved_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="`end` must be after `start`.",
        )

    range_days = (resolved_end - resolved_start).days
    if range_days > MAX_EXPORT_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"The requested date range spans {range_days} days, which exceeds the "
                f"{MAX_EXPORT_RANGE_DAYS}-day maximum for a single export -- narrow the "
                "range and export in multiple batches if you need more history."
            ),
        )

    rows, total = await moderation_log_service.list_team_log(
        db, start=resolved_start, end=resolved_end, limit=MAX_EXPORT_ROWS, offset=0
    )
    if total > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{total} entries match this date range, which exceeds the "
                f"{MAX_EXPORT_ROWS}-row maximum for a single export -- narrow the date "
                "range and export in multiple batches instead."
            ),
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["Timestamp (UTC)", "Actor Email", "Actor Full Name", "Actor Role", "Action", "Target Type", "Target ID", "Outcome (JSON)"]
    )
    for entry, actor_email, actor_full_name in rows:
        target_type_cell, target_id_cell = _csv_cell_target(entry.target_type, entry.target_id)
        writer.writerow(
            [
                entry.created_at.isoformat(),
                actor_email or "",
                actor_full_name or "",
                entry.actor_role.value if entry.actor_role else "",
                entry.action.value,
                target_type_cell,
                target_id_cell,
                # Step 1.3/Step 2 flattening decision: `outcome` stays a
                # single raw JSON-string cell rather than being split
                # into per-key columns. It deliberately varies shape per
                # action (see `ModerationLogEntry.outcome`'s own column
                # docstring) -- there is no one fixed set of sub-keys
                # every action shares, so flattening it into columns
                # would mean a different, ever-growing column set per
                # action type. `csv.writer` already handles the escaping
                # (embedded commas/quotes/newlines inside the JSON string
                # are quoted correctly since the whole string is one
                # field), so this is real, correct, importable CSV, not
                # a lossy approximation.
                json.dumps(entry.outcome, ensure_ascii=False, sort_keys=True),
            ]
        )
    csv_content = buffer.getvalue()

    # Step 3.3: log the export itself -- see this module's own docstring
    # and `ModerationAction.ACTIVITY_LOG_EXPORT`'s own docstring for the
    # full reasoning on why `target_type=USER`/`target_id=<self>`.
    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.ACTIVITY_LOG_EXPORT,
        target_type=ModerationTargetType.USER,
        target_id=current_user.id,
        outcome={
            "start": resolved_start.isoformat(),
            "end": resolved_end.isoformat(),
            "row_count": total,
            "format": format,
        },
    )
    await db.commit()

    # Sanitized filename -- same "a value that flows unescaped into a
    # Content-Disposition header is not safe" reasoning
    # `deadline_alerts.py::export_ics` already established, applied here
    # to the date-range components instead of a user-supplied name.
    safe_start = re.sub(r"[^A-Za-z0-9_.-]", "_", resolved_start.date().isoformat())
    safe_end = re.sub(r"[^A-Za-z0-9_.-]", "_", resolved_end.date().isoformat())
    filename = f"moderation_log_{safe_start}_to_{safe_end}.csv"

    return StreamingResponse(
        io.BytesIO(csv_content.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
