"""Repeat-Offender Detection -- service layer.

Investigation summary (this step's own Step 1/2, kept here as the real,
durable record of the design decision rather than only in a chat reply):

Every real "N times against this person" fact this feature can honestly
report has to be resolved from `ModerationLogEntry` rows, because that is
the ONLY durable moderation record in this codebase -- `chat_security`'s
Redis bans are TTL'd and self-expire with nothing left behind (confirmed:
`chat_security.ban_user`'s automatic Arabic-filter call site wrote no log
entry at all before this step; see `ModerationAction.CHAT_AUTO_BAN`'s own
docstring for the fix, now wired at that exact call site in
`app/api/v1/endpoints/chat.py`). `ModerationLogEntry.target_id` is a
deliberately-polymorphic, non-FK column (see that model's own docstring),
so "who was this action actually taken against" is resolved differently
per `ModerationAction` member, confirmed by re-reading every real call
site (`admin_moderation.py`, `admin_chat_reports.py`, `chat.py`):

- `VIDEO_DELETE` -- the `StudentVideo` row is gone by the time the log
  entry is written, so the offender was captured directly into
  `outcome["student_id"]` at write time (already true before this step;
  no change needed).
- `VIDEO_OVERRIDE` / `VIDEO_REJECT_AND_PURGE` -- the `StudentVideo` row
  SURVIVES the action (an override can be reversed later; a
  reject-and-purge only deletes the media, not the row), so the real
  offender is recovered with a live join: `target_id` IS the video id,
  `StudentVideo.student_id` is the owner. `VIDEO_OVERRIDE` additionally
  needs its own `outcome["new_status"]` checked -- an override that
  approved a video is not punitive against its owner and must NOT count
  as an offense (a moderator correcting an earlier wrongful rejection
  should never make the video's own owner look like a repeat offender).
- `CHAT_MESSAGE_DELETE` -- the `ChatMessage` row IS deleted by the
  route, so its sender was captured into `outcome["sender_id"]` at write
  time (already true; no change needed).
- `CHAT_REPORT_ESCALATE_BAN` -- `outcome["banned_user_id"]` (already
  true; no change needed).
- `CHAT_AUTO_BAN` -- the new member this step adds -- direct target:
  `target_type == USER`, `target_id` IS the banned user, same convention
  `CHAT_UNBAN` already uses (no `outcome` extraction needed).
- `CHAT_UNBAN` and `CHAT_REPORT_DISMISS` are deliberately EXCLUDED --
  neither is punitive against a real offender. An unban is mitigating (it
  ends a punishment early); a dismissed report means a human reviewer
  decided the report was baseless, so counting it would penalize the
  REPORTED person for something a moderator already concluded didn't
  happen.

DISCLOSED IMPLEMENTATION CHOICE: offender resolution is done in PYTHON,
not via a single SQL query with JSONB `->>` path operators, even though
`outcome` is a real JSONB column. This codebase's own test suite already
carries a confirmed, disclosed SQLite/JSONB compiler gap (see
`test_student_videos.py`/`test_live_session_reminders.py`, both erroring
on JSONB path expressions against the SQLite test backend) -- writing a
NEW JSONB-path query here would silently inherit that same gap and make
every real unit test for this feature uncollectable, not just slow. A
plain `SELECT ... WHERE action IN (...) AND created_at >= :cutoff` (no
JSONB operators at all) plus a small number of `StudentVideo` id lookups,
resolved in Python, works identically against SQLite and real Postgres
and keeps this feature's own tests portable -- the real audit log this
scans is bounded by the lookback window by construction (see
`REPEAT_OFFENDER_LOOKBACK_DAYS` below), so this is not a full-table scan
in practice.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ModerationAction, ModerationTargetType
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo

# Named constant, per this step's own explicit "a real named constant,
# not hardcoded inline" instruction. 30 days -- long enough that a real
# pattern of repeated moderation actions against the same person shows up
# (a single bad week doesn't get lost immediately), short enough that
# someone who made one mistake a year ago isn't still flagged as a
# "repeat offender" forever. Matches this step's own illustrative example
# ("moderated 3 times in the last 30 days").
REPEAT_OFFENDER_LOOKBACK_DAYS = 30

# Named constant -- the count AT OR ABOVE which a user is considered a
# real repeat offender. 3, matching this step's own illustrative example.
# Chosen (not just borrowed from the example) because it mirrors this
# codebase's OWN existing threshold reasoning for the chat-report dedup
# rule (`chat_report_service.py`'s "one real report per user per message
# is enough to surface it" -- a LOW bar for surfacing something to a
# human, since a human still reviews before anything punitive happens):
# 2 strikes is still plausibly "this could happen to anyone," 3 real,
# independent moderation actions against the same person inside 30 days
# is a genuine pattern worth a moderator's attention, not a false-positive
# risk the way 2 would be.
REPEAT_OFFENDER_THRESHOLD = 3

# The closed set of `ModerationAction` members counted as a real offense
# against a real person, per this module's own docstring above.
_PUNITIVE_ACTIONS: set[ModerationAction] = {
    ModerationAction.VIDEO_OVERRIDE,
    ModerationAction.VIDEO_DELETE,
    ModerationAction.VIDEO_REJECT_AND_PURGE,
    ModerationAction.CHAT_MESSAGE_DELETE,
    ModerationAction.CHAT_REPORT_ESCALATE_BAN,
    ModerationAction.CHAT_AUTO_BAN,
}


async def _resolve_offenders(
    db: AsyncSession, entries: list[ModerationLogEntry]
) -> dict[uuid.UUID, uuid.UUID]:
    """Maps each entry's `id` to the real offending user id, per this
    module's own per-action-type resolution rules. Entries this can't
    honestly resolve an offender for (a non-punitive `VIDEO_OVERRIDE`
    that approved rather than rejected, or -- a real, disclosed edge
    case -- a `VIDEO_OVERRIDE`/`VIDEO_REJECT_AND_PURGE` whose
    `StudentVideo` row was SUBSEQUENTLY hard-deleted by a later
    `VIDEO_DELETE`, which is possible since these are independent rows
    with no FK) are simply absent from the returned mapping rather than
    guessed at.
    """
    offenders: dict[uuid.UUID, uuid.UUID] = {}

    # Direct-target entries (no lookup needed): the offender IS
    # `target_id` (`CHAT_AUTO_BAN`) or was captured into `outcome` at
    # write time (`VIDEO_DELETE`, `CHAT_MESSAGE_DELETE`,
    # `CHAT_REPORT_ESCALATE_BAN`).
    video_join_ids: set[uuid.UUID] = set()
    for entry in entries:
        if entry.action == ModerationAction.CHAT_AUTO_BAN:
            offenders[entry.id] = entry.target_id
        elif entry.action == ModerationAction.VIDEO_DELETE:
            student_id = entry.outcome.get("student_id")
            if student_id:
                offenders[entry.id] = uuid.UUID(student_id)
        elif entry.action == ModerationAction.CHAT_MESSAGE_DELETE:
            sender_id = entry.outcome.get("sender_id")
            if sender_id:
                offenders[entry.id] = uuid.UUID(sender_id)
        elif entry.action == ModerationAction.CHAT_REPORT_ESCALATE_BAN:
            banned_id = entry.outcome.get("banned_user_id")
            if banned_id:
                offenders[entry.id] = uuid.UUID(banned_id)
        elif entry.action == ModerationAction.VIDEO_REJECT_AND_PURGE:
            video_join_ids.add(entry.target_id)
        elif entry.action == ModerationAction.VIDEO_OVERRIDE:
            # Only a REJECTING override is punitive -- see this module's
            # own docstring.
            if entry.outcome.get("new_status") == "rejected":
                video_join_ids.add(entry.target_id)

    if video_join_ids:
        result = await db.execute(
            select(StudentVideo.id, StudentVideo.student_id).where(
                StudentVideo.id.in_(video_join_ids)
            )
        )
        video_owner_by_id = {video_id: student_id for video_id, student_id in result.all()}

        for entry in entries:
            if entry.id in offenders:
                continue
            if entry.action not in (
                ModerationAction.VIDEO_REJECT_AND_PURGE,
                ModerationAction.VIDEO_OVERRIDE,
            ):
                continue
            owner_id = video_owner_by_id.get(entry.target_id)
            if owner_id is not None:
                offenders[entry.id] = owner_id

    return offenders


async def get_recent_offense_counts(
    db: AsyncSession, *, window_days: int = REPEAT_OFFENDER_LOOKBACK_DAYS
) -> dict[uuid.UUID, int]:
    """Real counts of punitive moderation actions per real offending user,
    within the last `window_days` -- the one query both real Step 3.2
    surfaces (the inline per-user check AND the Owner's flagged-users
    list) share, so the two views can never disagree about what counts.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)

    result = await db.execute(
        select(ModerationLogEntry).where(
            ModerationLogEntry.action.in_(_PUNITIVE_ACTIONS),
            ModerationLogEntry.created_at >= cutoff,
        )
    )
    entries = list(result.scalars().all())

    offenders = await _resolve_offenders(db, entries)

    counts: dict[uuid.UUID, int] = {}
    for offender_id in offenders.values():
        counts[offender_id] = counts.get(offender_id, 0) + 1
    return counts


async def get_recent_offense_count_for_user(
    db: AsyncSession, user_id: uuid.UUID, *, window_days: int = REPEAT_OFFENDER_LOOKBACK_DAYS
) -> int:
    """Real count for exactly ONE user -- backs the inline
    "this user has been moderated N times in the last 30 days" context
    shown while reviewing a video or chat report (Step 3.2's real-time
    surfacing decision). Built on the exact same `get_recent_offense_counts`
    query as the Owner's flagged-users list, just indexed down to one id,
    so an inline count and the flagged-list count for the same user can
    never disagree.
    """
    counts = await get_recent_offense_counts(db, window_days=window_days)
    return counts.get(user_id, 0)


async def get_flagged_users(
    db: AsyncSession,
    *,
    window_days: int = REPEAT_OFFENDER_LOOKBACK_DAYS,
    threshold: int = REPEAT_OFFENDER_THRESHOLD,
) -> list[tuple[uuid.UUID, int]]:
    """Real users at or above `threshold` real offenses within
    `window_days`, sorted by count descending -- the Owner-pull "flagged
    users" list (Step 3.2's second real surface), matching the user's own
    established "I check in on my own schedule, no notifications"
    preference: this is a real, pollable list, not a push alert.
    """
    counts = await get_recent_offense_counts(db, window_days=window_days)
    flagged = [(user_id, count) for user_id, count in counts.items() if count >= threshold]
    flagged.sort(key=lambda pair: pair[1], reverse=True)
    return flagged
