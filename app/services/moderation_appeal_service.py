"""Transparency: Notify Users of Moderation Actions + Real Appeal Path --
service layer.

STEP 3.3'S OWN REQUIRED HONEST DESIGN, re-derived here as the single
source of truth both the appeal-submission endpoint (to set
`ModerationAppeal.can_be_restored` at creation time) and the
appeal-resolution endpoint (to decide what upholding an appeal actually
DOES) share:

- `VIDEO_OVERRIDE` (only when it rejected, not approved -- an approving
  override has nothing to appeal) -- REVERSIBLE. The `StudentVideo` row
  and its media both still exist (an override never deletes anything);
  upholding the appeal genuinely flips `status` back to `APPROVED`,
  identical in effect to a fresh approving override.
- `CHAT_REPORT_ESCALATE_BAN` / `CHAT_AUTO_BAN` -- REVERSIBLE. The real,
  pre-existing `chat_security.unban_user` path (already used by
  `DELETE /chat/admin/bans/{user_id}`) can genuinely lift the ban, same
  mechanism, nothing new to invent. If the Redis TTL already expired
  naturally by the time the appeal is resolved, there's simply nothing
  left to lift -- still counts as upheld, just with an honest "the ban
  had already expired" outcome rather than a fabricated "we unbanned
  you" claim.
- `VIDEO_DELETE` -- NOT reversible. The row itself is gone (confirmed:
  `delete_video_permanently` calls `db.delete(video)`, and this
  codebase's Cloud Garbage Collection step then deletes the actual R2
  media too) -- there is no video left to restore `status` on, and no
  soft-delete/trash mechanism exists anywhere in this codebase to recover
  it from. Upholding this kind of appeal can only be an acknowledgment.
- `VIDEO_REJECT_AND_PURGE` -- NOT reversible, for a narrower but equally
  real reason: the `StudentVideo` row DOES survive (only `reject_video_
  and_purge_media` runs, which purges storage but keeps the row -- see
  that endpoint's own docstring), so flipping `status` back to `APPROVED`
  is technically possible, but the actual video file is already gone
  from R2 -- an "approved" row with no playable media would be a more
  misleading UI state than an honest "upheld, but your video's file
  could not be recovered" acknowledgment. Treated as acknowledgment-only
  for this reason, not because the DB operation is impossible.
- `CHAT_MESSAGE_DELETE` -- NOT reversible. `delete_reported_message`
  calls `db.delete(message)`, a real hard delete with no soft-delete
  column anywhere on `ChatMessage` -- the row and its content are simply
  gone.

Every other `ModerationAction` member (`CHAT_UNBAN`, `CHAT_REPORT_DISMISS`,
and this task's own new `APPEAL_UPHELD`/`APPEAL_DENIED`) is NOT
appealable at all -- the first two are neutral-or-positive outcomes with
no real grievance to appeal (see `repeat_offender_service.py`'s own
"never punitive" reasoning for the same two), and appealing a resolved
appeal itself is out of this task's scope (a real, disclosed future
extension point, not built here to avoid an unbounded appeal-of-an-
appeal chain with no natural termination).
"""

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AdminRole, ModerationAction, ModerationTargetType
from app.models.moderation_appeal import (
    APPEAL_STATUS_DENIED,
    APPEAL_STATUS_OPEN,
    APPEAL_STATUS_UPHELD,
    ModerationAppeal,
)
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo, StudentVideoStatus
from app.services import chat_security, moderation_log_service, notification_service, repeat_offender_service

logger = logging.getLogger(__name__)

# The closed set of actions with a real grievance to appeal at all --
# every action this codebase's own real notification triggers (see
# `admin_moderation.py`/`admin_chat_reports.py`/`chat.py`'s own
# "Transparency" comments) fire a notification for, minus the two
# neutral/positive ones (`CHAT_UNBAN`, `CHAT_REPORT_DISMISS`) that have
# nothing negative done to anyone to contest.
REVERSIBLE_ACTIONS: set[ModerationAction] = {
    ModerationAction.VIDEO_OVERRIDE,
    ModerationAction.CHAT_REPORT_ESCALATE_BAN,
    ModerationAction.CHAT_AUTO_BAN,
}
ACKNOWLEDGMENT_ONLY_ACTIONS: set[ModerationAction] = {
    ModerationAction.VIDEO_DELETE,
    ModerationAction.VIDEO_REJECT_AND_PURGE,
    ModerationAction.CHAT_MESSAGE_DELETE,
}
APPEALABLE_ACTIONS: set[ModerationAction] = REVERSIBLE_ACTIONS | ACKNOWLEDGMENT_ONLY_ACTIONS


class AppealNotAllowedError(Exception):
    """Raised when the referenced `ModerationLogEntry` isn't a real,
    appealable action at all (e.g. `CHAT_UNBAN`, or the log entry
    doesn't exist)."""


class NotTheAffectedUserError(Exception):
    """Raised when the caller isn't the real, resolved offender for this
    specific log entry -- an appeal is not a general-purpose complaint
    form; only the actual affected student may file one against their
    own case."""


class DuplicateOpenAppealError(Exception):
    """Raised when a real, still-OPEN appeal already exists for this log
    entry -- mirrors `chat_report_service.DuplicateReportError`'s own
    "don't let a second submission quietly fork the same case" posture.
    A RESOLVED appeal does not block a fresh one (see this module's own
    `create_appeal` docstring)."""


def is_appealable(action: ModerationAction) -> bool:
    return action in APPEALABLE_ACTIONS


async def create_appeal(
    db: AsyncSession,
    *,
    log_entry: ModerationLogEntry,
    student_id: uuid.UUID,
    message: str,
    additional_context: str | None,
) -> ModerationAppeal:
    """Real validation, in order: (1) the action is genuinely appealable
    at all, (2) the caller IS the real, resolved offender for this exact
    entry (reusing `repeat_offender_service.resolve_offender_for_entry`
    -- the SAME per-action-type resolution logic already tested for
    Repeat-Offender Detection, not a second, independent implementation
    that could drift from it), (3) no other OPEN appeal already exists
    for this entry.

    A student may file a FRESH appeal against an entry that already has
    a RESOLVED (upheld/denied) one -- e.g. new evidence surfaces after a
    denial. This is a real, deliberate choice mirroring `chat_report_
    service.create_report`'s own "a dismissed case can be re-opened by a
    fresh report" precedent, not an oversight.
    """
    if not is_appealable(log_entry.action):
        raise AppealNotAllowedError(log_entry.action.value)

    real_offender_id = await repeat_offender_service.resolve_offender_for_entry(db, log_entry)
    if real_offender_id is None or real_offender_id != student_id:
        raise NotTheAffectedUserError(str(log_entry.id))

    existing = await db.execute(
        select(ModerationAppeal).where(
            ModerationAppeal.log_entry_id == log_entry.id,
            ModerationAppeal.status == APPEAL_STATUS_OPEN,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise DuplicateOpenAppealError(str(log_entry.id))

    appeal = ModerationAppeal(
        id=uuid.uuid4(),
        log_entry_id=log_entry.id,
        student_id=student_id,
        message=message,
        additional_context=additional_context,
        status=APPEAL_STATUS_OPEN,
        can_be_restored=log_entry.action in REVERSIBLE_ACTIONS,
    )
    db.add(appeal)
    await db.commit()
    await db.refresh(appeal)
    return appeal


async def resolve_appeal(
    db: AsyncSession,
    *,
    appeal: ModerationAppeal,
    log_entry: ModerationLogEntry,
    decision: str,
    resolver_id: uuid.UUID,
    resolver_role: AdminRole,
    notes: str | None,
) -> tuple[ModerationAppeal, bool, str]:
    """Performs the real decision, in order:

    `deny` -- no reversal of any kind; just records the decision.

    `uphold` on a REVERSIBLE action -- performs the REAL underlying
    reversal (flips `StudentVideo.status` back to `APPROVED`, or calls
    the real `chat_security.unban_user`), then records it.

    `uphold` on an ACKNOWLEDGMENT-ONLY action -- records the decision
    with NO reversal action attempted (there is nothing left to restore
    -- see this module's own docstring). Returns `reversal_applied=False`
    even though the appeal's own `status` is `upheld`, so a caller can
    tell "we agreed you were wronged" apart from "and we fixed it" --
    two different real facts this schema/response deliberately does not
    conflate.

    Returns `(appeal, reversal_applied, reversal_detail)` -- the detail
    string is the real, honest human-readable outcome used both in the
    `AppealResolveResponse` and in the notification sent back to the
    student (see the router's own call site).
    """
    reversal_applied = False
    reversal_detail = "No reversal applicable to this decision."

    if decision == "deny":
        appeal.status = APPEAL_STATUS_DENIED
        reversal_detail = "The appeal was reviewed and the original decision was upheld as correct."
        action = ModerationAction.APPEAL_DENIED
    else:
        appeal.status = APPEAL_STATUS_UPHELD
        action = ModerationAction.APPEAL_UPHELD

        if log_entry.action == ModerationAction.VIDEO_OVERRIDE:
            video = await db.get(StudentVideo, log_entry.target_id)
            if video is not None:
                video.status = StudentVideoStatus.APPROVED.value
                db.add(video)
                reversal_applied = True
                reversal_detail = "Your video has been restored to APPROVED status."
            else:
                reversal_detail = (
                    "The appeal was upheld, but the video no longer exists (it was removed by a "
                    "separate, later action), so its status could not be restored."
                )
        elif log_entry.action in (ModerationAction.CHAT_REPORT_ESCALATE_BAN, ModerationAction.CHAT_AUTO_BAN):
            # `unban_user` itself returns whether a real key existed to
            # delete (see its own docstring) -- reused directly rather
            # than a separate `is_user_banned` check first, avoiding a
            # real (if narrow) TOCTOU race between the two Redis calls.
            was_banned = await chat_security.unban_user(appeal.student_id)
            if was_banned:
                reversal_applied = True
                reversal_detail = "Your chat ban has been lifted."
            else:
                reversal_detail = "The appeal was upheld, but your ban had already expired on its own."
        else:
            # ACKNOWLEDGMENT_ONLY_ACTIONS -- deliberately no reversal
            # attempt; see this module's own docstring for why each of
            # these three specifically cannot restore real content.
            if log_entry.action == ModerationAction.VIDEO_DELETE:
                reversal_detail = (
                    "The appeal was upheld -- the removal is acknowledged as a mistake -- but the video "
                    "was permanently deleted and cannot be restored."
                )
            elif log_entry.action == ModerationAction.VIDEO_REJECT_AND_PURGE:
                reversal_detail = (
                    "The appeal was upheld -- the removal is acknowledged as a mistake -- but the video's "
                    "media file was already permanently removed and cannot be restored."
                )
            elif log_entry.action == ModerationAction.CHAT_MESSAGE_DELETE:
                reversal_detail = (
                    "The appeal was upheld -- the removal is acknowledged as a mistake -- but the message "
                    "was permanently deleted and cannot be restored."
                )

    appeal.resolved_by = resolver_id
    appeal.resolved_at = dt.datetime.now(dt.timezone.utc)
    appeal.resolution_notes = notes
    db.add(appeal)

    moderation_log_service.log_action(
        db,
        actor_id=resolver_id,
        actor_role=resolver_role,
        action=action,
        target_type=ModerationTargetType.MODERATION_APPEAL,
        target_id=appeal.id,
        outcome={"reversal_applied": reversal_applied, "reversal_detail": reversal_detail, "notes": notes},
    )

    await db.commit()
    await db.refresh(appeal)

    try:
        await notification_service.create_notification(
            db,
            appeal.student_id,
            "WARNING",
            "Your appeal has been reviewed",
            reversal_detail,
            payload={"type": "appeal_resolved", "appeal_id": str(appeal.id), "status": appeal.status},
        )
    except Exception:
        logger.exception("Notifications: failed to notify student %s of appeal %s resolution", appeal.student_id, appeal.id)

    return appeal, reversal_applied, reversal_detail
