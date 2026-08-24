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
