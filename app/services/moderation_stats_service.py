"""Phase 5 (Contribution Stats + The 3 Views) -- service layer for the
two new aggregation endpoints (`GET /admin/team/summary`, `GET
/admin/today`). Builds on the confirmed-real activity log (commit
5d2934e) and claim system (commit 47906f0) -- reuses `ModerationLogEntry`
and `moderation_claim_service.get_effective_claim`/`CLAIM_TIMEOUT_MINUTES`
directly rather than re-deriving either.

STEP 1 -- THE REAL RESPONSE-TIME DEFINITION, AND WHY
------------------------------------------------------
Two real, DB-backed timestamps exist that could plausibly define
"response time": `StudentVideo.claimed_at` (Phase 4) and
`ModerationLogEntry.created_at` (Phase 3), each pointing at
`StudentVideo.created_at` (the moment a video's row is created, which IS
"entered the review queue" -- confirmed by re-reading
`app/models/student_videos.py` fully: a video's row is created
immediately at upload, starting in `PENDING_AI`; there is no separate
upload/submission timestamp distinct from `created_at`).

This service deliberately measures ONE historical metric --
`ModerationLogEntry.created_at - StudentVideo.created_at`, the
"queue-entry-to-action" gap -- NOT a claim-based
"queue-entry-to-claim" or "claim-to-action" gap, for a real, disclosed
reason: `StudentVideo.claimed_by`/`claimed_at` are MUTABLE, LIVE state,
not an append-only log. `moderation_claim_service.apply_release` sets
`claimed_at` back to `None`, and a later `apply_claim` overwrites it
again. `ModerationLogEntry` never captured claim state in its own
`outcome` JSONB at write time (see that model's own docstring on why
`outcome` holds each action's own result, not the claim state
surrounding it). This means that by the time a WEEKLY or MONTHLY
aggregation query runs, `StudentVideo.claimed_at` reflects only
whatever claim (if any) happens to be live on that row RIGHT NOW -- it
may have been reclaimed, released, or expired one or more times since
whichever historical log entry an aggregation query is trying to
attribute a gap to. A claim-based historical metric would silently mix
real, current-claim data for some rows with stale-or-absent data for
others, with no way to tell the two apart after the fact. This is
exactly the "don't invent a metric you can't actually calculate from
real data" trap this step's own ask warns against.

`StudentVideo.created_at`, by contrast, is immutable (no `updated_at`
on this model at all) and durable for exactly as long as the row
itself exists -- which is where the real, disclosed limitation below
comes in.

REAL, DISCLOSED LIMITATION: the queue-entry-to-action gap is
NOT computable for every logged action:
- `ModerationAction.VIDEO_DELETE` (`delete_video_permanently`,
  `admin_moderation.py`) does a real hard `db.delete(video)` -- by the
  time any later aggregation query runs, the `StudentVideo` row a
  `VIDEO_DELETE` log entry's `target_id` refers to no longer exists.
  The join below is an INNER JOIN specifically so these rows drop out
  of the response-time sample cleanly (not a `None`-in-the-average) --
  they still count toward `total_actions`/`action_counts`, just not
  toward the response-time sample.
- `ModerationAction.CHAT_UNBAN` (`chat.py`) targets a `User`
  (`ModerationTargetType.USER`), which has no "entered the review
  queue" timestamp at all -- there is no real queue a chat ban was ever
  "in." Excluded from the join by the `target_type ==
  ModerationTargetType.STUDENT_VIDEO` filter below, same reason.

So `avg_response_time_seconds` is a real, honest average over
`VIDEO_OVERRIDE`/`VIDEO_REJECT_AND_PURGE` actions only (the two where
the acted-on `StudentVideo` row still exists at aggregation time) --
never silently padded with an assumed value for the actions it can't
cover.

A SEPARATE, LIVE (not historical) claim-based metric IS surfaced, per
Step 3's own literal ask -- see `get_today_digest`'s own docstring for
"longest current wait time on an unclaimed/unactioned queue item,"
computed directly against the review queue's CURRENT state, where
`claimed_at` mutability is a non-issue (there's nothing historical
being reconstructed).
"""

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ModerationAction, ModerationTargetType, StudentVideoStatus
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services.moderation_claim_service import CLAIM_TIMEOUT_MINUTES

# The two real actions a "queue-entry-to-action" gap is honestly
# computable for -- see this module's own docstring. Named as a real
# constant (not repeated inline) so the one disclosed exclusion rule
# lives in exactly one place.
_RESPONSE_TIME_ELIGIBLE_ACTIONS = (
    ModerationAction.VIDEO_OVERRIDE,
    ModerationAction.VIDEO_REJECT_AND_PURGE,
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def period_bounds(period: str, *, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """Real, server-computed `[start, end)` window for `period`, per
    `GET /admin/team/summary`'s own disclosed design call (this endpoint
    computes "the current week/month" server-side, unlike `GET
    /admin/activity-log`'s deliberately client-computed `start`/`end` --
    a dedicated "current period" summary is exactly the case that
    endpoint's own docstring says WOULD justify a dedicated endpoint:
    different, fixed aggregation semantics, not a second way to express
    the same generic date-range filter).

    `"week"` -- the current ISO week: Monday 00:00 UTC through the
    FOLLOWING Monday 00:00 UTC (exclusive), containing `now`. Chosen
    over a rolling trailing-7-days window because "week" throughout
    this codebase's own real precedent (`Leaderboard`'s monthly reset,
    `list_team_log`'s own worked example of "this week" as "most recent
    Monday 00:00") already means calendar-aligned, not rolling.

    `"month"` -- the current calendar month: the 1st at 00:00 UTC
    through the 1st of the FOLLOWING month at 00:00 UTC (exclusive).

    Both bounds are UTC -- same disclosed posture as `list_team_log`'s
    own docstring on why no server-side local-timezone guess is baked
    in for a team that may span timezones; UTC is the one real,
    unambiguous default every volunteer's client can consistently
    convert away from.

    Raises `ValueError` for anything other than `"week"`/`"month"` --
    the router's own `Literal["week", "month"]` query-param type already
    makes this unreachable via the real endpoint, but this function
    stays independently defensive since it's also called directly by
    the real unit tests.
    """
    current = now if now is not None else _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)

    if period == "week":
        start_date = current.date() - dt.timedelta(days=current.weekday())
        start = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=7)
        return start, end

    if period == "month":
        start = dt.datetime(current.year, current.month, 1, tzinfo=dt.timezone.utc)
        if current.month == 12:
            end = dt.datetime(current.year + 1, 1, 1, tzinfo=dt.timezone.utc)
        else:
            end = dt.datetime(current.year, current.month + 1, 1, tzinfo=dt.timezone.utc)
        return start, end

    raise ValueError(f"Unknown period: {period!r} (expected 'week' or 'month')")


@dataclass
class VolunteerSummaryRow:
    actor_id: uuid.UUID | None
    actor_email: str | None
    actor_full_name: str | None
    action_counts: dict[str, int]
    total_actions: int
    avg_response_time_seconds: float | None
    response_time_sample_count: int


async def get_team_summary(
    db: AsyncSession, *, start: dt.datetime, end: dt.datetime
) -> list[VolunteerSummaryRow]:
    """Two real, separate `GROUP BY` aggregation queries (not
    fetch-all-rows-then-tally-in-Python) over `[start, end)`, merged in
    Python by `actor_id` -- this codebase's activity/claim data volume
    is genuinely small (one row per real moderation action, by a small
    volunteer team; see `ModerationLogEntry`'s own docstring), but the
    two GROUP BYs below are still the real, correct way to compute a
    count/average -- SQL is where a `COUNT`/`AVG` belongs regardless of
    row count, and doing it there means this query's cost stays
    O(matching rows) instead of O(all rows ever logged) as this table
    grows, with no code change needed later.

    Query 1 -- per-actor, per-action counts:
        SELECT actor_id, email, full_name, action, COUNT(*)
        FROM moderation_log_entries LEFT OUTER JOIN users ...
        WHERE created_at >= :start AND created_at < :end
        GROUP BY actor_id, email, full_name, action

    LEFT OUTER JOIN against `users` -- same "a deleted actor is a real,
    expected case" reasoning as `list_actor_log`/`list_team_log`'s own
    joins (`actor_id` is `ondelete=SET NULL`). `email`/`full_name` are
    included in the `GROUP BY` alongside `actor_id` -- functionally
    dependent on it (one user has exactly one current email/name), so
    this doesn't fragment the grouping; it's the standard way to carry
    a joined display column through a `GROUP BY` without a second query
    or a non-standard `ANY_VALUE`-style aggregate.

    Query 2 -- per-actor response-time SAMPLES, over ONLY the two real
    response-time-eligible actions (see this module's own docstring):
        SELECT actor_id, log.created_at, video.created_at
        FROM moderation_log_entries INNER JOIN student_videos ON student_videos.id = target_id
        WHERE target_type = 'student_video' AND action IN (...) AND created_at >= :start AND < :end

    Deliberately an INNER JOIN here (not LEFT) -- see this module's own
    docstring on why a `VIDEO_DELETE` row's target no longer exists;
    an INNER JOIN is what correctly, silently excludes it from the
    response-time sample without a manual `NULL`-filtering pass in
    Python.

    DISCLOSED, DELIBERATE DEVIATION from Query 1's own "aggregate in
    SQL, not Python" posture: the actual `AVG(gap_seconds)` here IS
    computed in Python, not via `AVG(EXTRACT(EPOCH FROM ...))` in SQL.
    The WHERE/JOIN filtering above -- the genuinely expensive, row-
    count-scaling part -- still happens entirely in SQL; this query
    already returns only the small, real subset of rows the response-
    time metric is even defined for (see this module's own docstring),
    not "all rows." Computing the final average over that already-
    narrow result set in Python, rather than via a DB-side
    `EXTRACT(EPOCH FROM ...)`, is a real, tested portability call:
    `EXTRACT(EPOCH FROM interval)` is genuine Postgres syntax (this
    codebase's real deployment target), but this codebase's own test
    suite runs every DB-touching test against in-memory SQLite (see
    every other test file's own `db_session_factory` fixture) --
    confirmed directly, not assumed, that SQLAlchemy's `func.extract
    ("epoch", ...)` against a raw datetime SUBTRACTION does NOT
    translate to a correct interval-seconds value under SQLite (it
    silently returns numerically-wrong results there, no error raised
    -- the kind of bug this task's own "don't invent a metric you can't
    actually calculate" instruction is exactly about avoiding). Doing
    the final division/average in Python is correct and identical on
    both dialects, at the cost of transferring only as many rows as
    this actor/period genuinely has response-time-eligible actions for
    -- never the full table.
    """
    count_filters = (
        ModerationLogEntry.created_at >= start,
        ModerationLogEntry.created_at < end,
    )
    count_result = await db.execute(
        select(
            ModerationLogEntry.actor_id,
            User.email,
            User.full_name,
            ModerationLogEntry.action,
            func.count().label("cnt"),
        )
        .outerjoin(User, User.id == ModerationLogEntry.actor_id)
        .where(*count_filters)
        .group_by(ModerationLogEntry.actor_id, User.email, User.full_name, ModerationLogEntry.action)
    )

    rows_by_actor: dict[uuid.UUID | None, VolunteerSummaryRow] = {}
    for actor_id, email, full_name, action, cnt in count_result.all():
        row = rows_by_actor.get(actor_id)
        if row is None:
            row = VolunteerSummaryRow(
                actor_id=actor_id,
                actor_email=email,
                actor_full_name=full_name,
                action_counts={},
                total_actions=0,
                avg_response_time_seconds=None,
                response_time_sample_count=0,
            )
            rows_by_actor[actor_id] = row
        action_value = action.value if hasattr(action, "value") else action
        row.action_counts[action_value] = cnt
        row.total_actions += cnt

    response_time_filters = (
        ModerationLogEntry.created_at >= start,
        ModerationLogEntry.created_at < end,
        ModerationLogEntry.target_type == ModerationTargetType.STUDENT_VIDEO,
        ModerationLogEntry.action.in_(_RESPONSE_TIME_ELIGIBLE_ACTIONS),
    )
    response_time_result = await db.execute(
        select(
            ModerationLogEntry.actor_id,
            ModerationLogEntry.created_at,
            StudentVideo.created_at,
        )
        .join(StudentVideo, StudentVideo.id == ModerationLogEntry.target_id)
        .where(*response_time_filters)
    )

    gap_totals: dict[uuid.UUID | None, list[float]] = {}
    for actor_id, log_created_at, video_created_at in response_time_result.all():
        if log_created_at.tzinfo is None:
            log_created_at = log_created_at.replace(tzinfo=dt.timezone.utc)
        if video_created_at.tzinfo is None:
            video_created_at = video_created_at.replace(tzinfo=dt.timezone.utc)
        gap_totals.setdefault(actor_id, []).append((log_created_at - video_created_at).total_seconds())

    for actor_id, gaps in gap_totals.items():
        row = rows_by_actor.get(actor_id)
        if row is None:
            # A real, reachable edge case: every one of this actor's
            # response-time-eligible actions in this window happened,
            # but the actor row itself was never counted above -- can't
            # actually happen (the count query has no action-type
            # filter, so it always sees every row the response-time
            # query does too), kept only as defensive dead code rather
            # than an unguarded KeyError.
            continue
        row.avg_response_time_seconds = sum(gaps) / len(gaps)
        row.response_time_sample_count = len(gaps)

    return sorted(
        rows_by_actor.values(),
        key=lambda r: (-r.total_actions, r.actor_full_name or ""),
    )


@dataclass
class TodayDigest:
    date: dt.date
    window_start: dt.datetime
    window_end: dt.datetime
    total_actions: int
    active_volunteer_count: int
    longest_unclaimed_wait_seconds: float | None
    longest_unclaimed_item_id: uuid.UUID | None


async def get_today_digest(db: AsyncSession, *, now: dt.datetime | None = None) -> TodayDigest:
    """Step 3's single real daily-digest payload.

    `date`/window -- real UTC calendar day containing `now` (`[00:00,
    24:00)` UTC) -- same disclosed "UTC is the one unambiguous default"
    reasoning as `period_bounds` above, and the same real reason this
    step's own single-payload, no-query-param endpoint needs a
    server-computed "today" at all (unlike `GET /admin/activity-log`,
    which takes `start`/`end` precisely so it never has to guess).

    `total_actions`/`active_volunteer_count` -- one real query:
    `COUNT(*)`/`COUNT(DISTINCT actor_id)` over
    `moderation_log_entries` in today's window.

    `longest_unclaimed_wait_seconds`/`longest_unclaimed_item_id` -- per
    this step's own literal "longest current wait time on an
    unclaimed/unactioned queue item" wording, this is a LIVE snapshot
    against the review queue's CURRENT state (`_REVIEW_QUEUE_STATUSES`
    from `admin_moderation.py`, re-expressed here rather than imported,
    since importing a route-module constant into the service layer
    would invert this codebase's own Models/Schemas -> Service ->
    Router layering), NOT a historical aggregate -- so the claim-data-
    mutability problem this module's own docstring raises for
    HISTORICAL metrics doesn't apply here; "unclaimed right now" is
    exactly what `claimed_by IS NULL OR claimed_at` is expired
    correctly answers when read at request time. Mirrors
    `moderation_claim_service.get_effective_claim`'s own expiry rule
    directly in SQL via the SAME real `CLAIM_TIMEOUT_MINUTES` constant
    (imported, not re-declared) rather than fetching every queue row
    into Python and re-deriving expiry per-row -- the "unclaimed or
    expired" condition is a plain, sargable `WHERE` clause, so pushing
    it into SQL is the real correct call here, not premature
    optimization.
    """
    current = now if now is not None else _now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)

    window_start = dt.datetime(current.year, current.month, current.day, tzinfo=dt.timezone.utc)
    window_end = window_start + dt.timedelta(days=1)

    totals_result = await db.execute(
        select(func.count(), func.count(func.distinct(ModerationLogEntry.actor_id)))
        .where(ModerationLogEntry.created_at >= window_start, ModerationLogEntry.created_at < window_end)
    )
    total_actions, active_volunteer_count = totals_result.one()

    claim_cutoff = current - dt.timedelta(minutes=CLAIM_TIMEOUT_MINUTES)
    review_queue_statuses = [
        StudentVideoStatus.REJECTED.value,
        StudentVideoStatus.FAILED.value,
        StudentVideoStatus.PENDING_AI.value,
    ]
    oldest_unclaimed_result = await db.execute(
        select(StudentVideo.id, StudentVideo.created_at)
        .where(
            StudentVideo.status.in_(review_queue_statuses),
            (StudentVideo.claimed_by.is_(None)) | (StudentVideo.claimed_at < claim_cutoff),
        )
        .order_by(StudentVideo.created_at.asc())
        .limit(1)
    )
    oldest_row = oldest_unclaimed_result.first()

    longest_wait_seconds: float | None = None
    longest_item_id: uuid.UUID | None = None
    if oldest_row is not None:
        item_id, item_created_at = oldest_row
        if item_created_at.tzinfo is None:
            item_created_at = item_created_at.replace(tzinfo=dt.timezone.utc)
        longest_wait_seconds = (current - item_created_at).total_seconds()
        longest_item_id = item_id

    return TodayDigest(
        date=window_start.date(),
        window_start=window_start,
        window_end=window_end,
        total_actions=total_actions,
        active_volunteer_count=active_volunteer_count,
        longest_unclaimed_wait_seconds=longest_wait_seconds,
        longest_unclaimed_item_id=longest_item_id,
    )
