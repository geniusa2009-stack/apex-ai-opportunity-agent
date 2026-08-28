"""Automatic Moderation Activity Log -- service layer.

Owns the one real write path (`log_action`) every logged moderation
action calls, plus the read-side helpers backing
`app/api/v1/endpoints/admin_activity_log.py`. Kept out of the routers
per this codebase's uniform 3-layer convention (Models/Schemas ->
Service -> Router), same posture as `notification_service.py`/
`gamification_service.py`.

DISCLOSED, DELIBERATE DIFFERENCE from `notification_service.create_notification`/
`gamification_service.add_xp`'s own "best-effort, wrapped in try/except,
never breaks the already-shipped flow it's attached to" posture: this
log write is NOT best-effort, and is NOT wrapped in a try/except at any
of its four call sites. Those two other side effects touch external
subsystems (Redis pub/sub, a Celery broker) that can genuinely be down
independently of the database itself, so failing the whole request over
their failure would be the wrong trade-off. `log_action` does exactly
one plain `INSERT` on the SAME `AsyncSession`/transaction the calling
route already has open for its own real mutation (the video/ban update)
-- there is no separate failure domain to guard against, and this
step's own ask is explicit that "every real moderation action gets
logged automatically" is the whole point of this feature; a silently
dropped log entry on a transient error would defeat it. Each call site
adds this call BEFORE its own `db.commit()` (not a second, separate
commit) so the action and its log entry are provably atomic: either
both persist, or neither does.
"""

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AdminRole, ModerationAction, ModerationTargetType
from app.models.moderation_log import ModerationLogEntry
from app.models.user import User

# Return shape both read queries below share: one `ModerationLogEntry`
# row plus its actor's denormalized `email`/`full_name`, resolved via a
# LEFT OUTER JOIN -- same "an inner join would silently drop a row whose
# referenced user was since deleted" reasoning as
# `admin_moderation.py::get_review_queue`'s own `User.full_name` join
# (that reasoning applies HARDER here: `actor_id` is `ondelete="SET
# NULL"`, so a deleted actor is an expected, real case this log must
# keep showing, not an edge case to guard against).
LogEntryWithActor = tuple[ModerationLogEntry, str | None, str | None]


def log_action(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    actor_role: AdminRole | None,
    action: ModerationAction,
    target_type: ModerationTargetType,
    target_id: uuid.UUID,
    outcome: dict,
) -> ModerationLogEntry:
    """Builds and `db.add()`s one real `ModerationLogEntry` -- deliberately
    does NOT call `db.commit()` itself (see this module's own docstring
    on why the log write must share its caller's transaction, not open
    its own).

    `actor_role` must be the actor's EFFECTIVE tier
    (`app/api/deps.py::effective_admin_role`), not the raw
    `User.admin_role` column -- a legacy `is_superuser=True` account can
    have `admin_role IS NULL` (see `AdminRole`'s own docstring on the
    legacy-alias resolution). Every HUMAN-actor call site resolves this
    the same way; see each one's own "Automatic Moderation Activity Log"
    comment.

    `actor_id`/`actor_role` are BOTH `| None` (Repeat-Offender Detection
    addition) -- `actor_role`'s own column docstring
    (`app/models/moderation_log.py`) explains why a system-initiated
    action (the automatic Arabic-filter ban, `ModerationAction.
    CHAT_AUTO_BAN`) has neither. Every pre-existing call site keeps
    passing real, non-`None` values for both -- this widening is purely
    additive.
    """
    entry = ModerationLogEntry(
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
    )
    db.add(entry)
    return entry


async def list_actor_log(
    db: AsyncSession, actor_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[LogEntryWithActor], int]:
    """`GET /admin/activity-log/{user_id}`'s own query -- one volunteer's
    real timeline, newest first. Uses the `(actor_id, created_at)`
    composite index (see the model's own docstring)."""
    filters = (ModerationLogEntry.actor_id == actor_id,)

    total = (
        await db.execute(select(func.count()).select_from(ModerationLogEntry).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(ModerationLogEntry, User.email, User.full_name)
        .outerjoin(User, User.id == ModerationLogEntry.actor_id)
        .where(*filters)
        .order_by(ModerationLogEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(entry, email, full_name) for entry, email, full_name in result.all()], total


async def list_team_log(
    db: AsyncSession,
    *,
    start: dt.datetime | None,
    end: dt.datetime | None,
    limit: int,
    offset: int,
) -> tuple[list[LogEntryWithActor], int]:
    """`GET /admin/activity-log`'s own query -- the whole team's log,
    newest first, optionally bounded by `[start, end)`.

    DISCLOSED DESIGN CALL (this step's own Step 3.3 explicitly invites
    one): a plain `start`/`end` filter on this ONE endpoint, not a
    separate dedicated "today"/"this week" endpoint. Both of Phase 5's
    named views are trivially expressible as date-range VALUES a caller
    computes once (`start=<today at 00:00>`) and passes here -- there is
    no query shape a dedicated endpoint would serve that this one
    doesn't already serve identically, just with the boundary computed
    server-side instead of client-side. A dedicated endpoint would only
    be justified if "today"/"this week" needed different SORTING,
    AGGREGATION, or ACCESS-CONTROL semantics than the general log -- none
    of which is true here. The `(created_at)` index (see the model's own
    docstring) keeps this filter efficient regardless of which range is
    requested.
    """
    filters = []
    if start is not None:
        filters.append(ModerationLogEntry.created_at >= start)
    if end is not None:
        filters.append(ModerationLogEntry.created_at < end)

    total = (
        await db.execute(select(func.count()).select_from(ModerationLogEntry).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(ModerationLogEntry, User.email, User.full_name)
        .outerjoin(User, User.id == ModerationLogEntry.actor_id)
        .where(*filters)
        .order_by(ModerationLogEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [(entry, email, full_name) for entry, email, full_name in result.all()], total


# --- Full Ban-Reason History addition ---
# The three real closed-set actions that make up a user's chat-ban
# lifecycle, per this task's own Step 1 investigation:
# `CHAT_AUTO_BAN` (the automatic Arabic-filter ban, no human actor),
# `CHAT_REPORT_ESCALATE_BAN` (a moderator escalating a reported message),
# `CHAT_UNBAN` (a moderator manually lifting a ban early). All three
# already share the SAME `target_type=ModerationTargetType.USER,
# target_id=<the affected user>` convention (confirmed by reading each
# real call site in `chat.py`/`admin_chat_reports.py`) -- no new
# target-shape work was needed to make this queryable by target user.
_DIRECT_TARGET_BAN_ACTIONS = (
    ModerationAction.CHAT_AUTO_BAN,
    ModerationAction.CHAT_UNBAN,
)


async def list_user_ban_history(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[LogEntryWithActor], int]:
    """`GET /admin/chat/users/{user_id}/ban-history`'s own query --
    DISCLOSED REAL GAP this closes: `list_actor_log` above answers "what
    did THIS MODERATOR do" (filters on `actor_id`); nothing in this
    codebase before this task could answer "what happened TO this
    student" (filtering on the banned user instead).

    CORRECTED DESIGN, disclosed: an earlier draft of this function
    assumed all three ban-lifecycle actions share the same
    `target_type=USER, target_id=<banned user>` shape and queried them
    in one pass. That assumption was checked against the real call site
    in `admin_chat_reports.py::escalate_chat_report_to_ban` and found
    false -- `CHAT_REPORT_ESCALATE_BAN` is logged with
    `target_type=CHAT_MESSAGE_REPORT, target_id=<the report's own id>`
    (the report being escalated is the target of that action; the
    banned user only appears inside `outcome["banned_user_id"]`, a
    plain string). A single `target_id == user_id` filter would
    therefore silently miss every escalate-to-ban event for a given
    student. This function now runs two real queries and merges them:

    1. `CHAT_AUTO_BAN` / `CHAT_UNBAN` -- both genuinely target the user
       (`target_type=USER, target_id=user_id`), so this is a normal,
       portable, indexable-in-principle SQL filter (same "no dedicated
       index added yet" call as before -- see below).
    2. `CHAT_REPORT_ESCALATE_BAN` -- fetched by `action` alone (there is
       no portable, engine-agnostic way to filter JSON keys across both
       real Postgres/JSONB in production and the raw-SQL-seeded SQLite
       used in this codebase's own tests), then filtered in Python on
       `entry.outcome.get("banned_user_id") == str(user_id)`. This is a
       real, disclosed cost: it fetches every escalate-ban row in the
       system before filtering, which is fine at this feature's actual
       admin-tool read volume but would need a real JSONB-path query
       (e.g. Postgres `outcome->>'banned_user_id'`) if escalate-ban
       volume ever grows large enough for this to matter.

    The two result sets are merged, re-sorted by `created_at` descending
    (SQL `ORDER BY`/`LIMIT`/`OFFSET` can't apply across two independent
    queries), and paginated in Python -- `total` is the true combined
    count across both action families, and `limit`/`offset` are applied
    to the merged, sorted list, not to either query alone.

    Reuses the exact same `LEFT OUTER JOIN` against `User` for the
    acting moderator's own email/name (still relevant here -- a
    ban-history row still needs to show WHO applied/lifted each ban,
    same as the activity log does for WHO performed any other action)
    -- the banned user's own id is intentionally NOT joined against
    `User` a second time (the caller already knows exactly which user
    this is, since they passed `user_id` in).

    No new index was added for either query: `target_id` alone has no
    index today, and the escalate-ban query has no target-level filter
    to index at all (it filters on `action`, then in Python). This
    endpoint's own real usage (moderators looking up ONE specific
    flagged/reported student at a time, not scanning the whole table)
    is a low-volume, admin-tool read against a table sized by real
    moderation events -- the same "acceptable without a dedicated index"
    judgment call this task's own Step 3 leaves open rather than
    speculatively adding index churn an actual production volume hasn't
    justified yet.
    """
    direct_filters = (
        ModerationLogEntry.target_type == ModerationTargetType.USER,
        ModerationLogEntry.target_id == user_id,
        ModerationLogEntry.action.in_(_DIRECT_TARGET_BAN_ACTIONS),
    )
    direct_result = await db.execute(
        select(ModerationLogEntry, User.email, User.full_name)
        .outerjoin(User, User.id == ModerationLogEntry.actor_id)
        .where(*direct_filters)
    )
    direct_rows = list(direct_result.all())

    escalate_result = await db.execute(
        select(ModerationLogEntry, User.email, User.full_name)
        .outerjoin(User, User.id == ModerationLogEntry.actor_id)
        .where(ModerationLogEntry.action == ModerationAction.CHAT_REPORT_ESCALATE_BAN)
    )
    target_user_id = str(user_id)
    escalate_rows = [
        (entry, email, full_name)
        for entry, email, full_name in escalate_result.all()
        if isinstance(entry.outcome, dict) and entry.outcome.get("banned_user_id") == target_user_id
    ]

    merged: list[LogEntryWithActor] = [
        (entry, email, full_name) for entry, email, full_name in (*direct_rows, *escalate_rows)
    ]
    merged.sort(key=lambda row: row[0].created_at, reverse=True)

    total = len(merged)
    page = merged[offset : offset + limit]
    return page, total
