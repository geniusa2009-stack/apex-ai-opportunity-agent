"""Two-Person Confirmation for Dangerous Actions -- service layer.

Owns:

1. The real pending-confirmation lifecycle (`request_confirmation`,
   `approve_confirmation`, `override_confirmation`, `get_active_pending`,
   `list_pending`) -- same layering convention as
   `moderation_escalation_service.py`/`case_note_service.py`.
2. The four real `_execute_*` mutation functions, MOVED HERE (not
   duplicated) from `admin_moderation.py`::`delete_video_permanently`/
   `reject_video_and_purge_media` and `admin_chat_reports.py`::
   `delete_reported_message` / `admin_guild_reports.py`::
   `delete_reported_guild_content`. Each one's real mutation logic
   (row/media deletion, cascade-safe status-before-delete ordering,
   `ModerationLogEntry`/notification shape) is UNCHANGED from before this
   task -- see each function's own docstring for exactly what moved.
   This is a deliberate layering fix, not just relocation for this
   feature's convenience: the four legacy routes need to call INTO this
   service (`request_confirmation`), and `approve_confirmation`/
   `override_confirmation` need to call the real mutation logic -- if
   that logic stayed in the router files, this service would import
   from the routers AND the routers would import from this service, a
   real circular import. Moving the mutation logic itself down into the
   service layer is exactly where `moderation_log_service.py`'s own
   docstring says mutation-adjacent logic belongs.

`actor_role: AdminRole` is threaded through every function here as an
EXPLICIT parameter, resolved by the caller (`app.api.deps.
effective_admin_role(current_user)`), NEVER recomputed inside this
module -- same established convention `admin_escalations.py`/
`admin_case_notes.py` already use (both resolve it once in the router,
pass the resolved enum down); this service module deliberately has NO
dependency on `app.api.deps` at all.

DISCLOSED SMALL DUPLICATION: the reason-summary helper for chat/guild
reports (`SELECT DISTINCT reason ... comma-joined`) is duplicated here,
unchanged, from `admin_chat_reports.py::_summarize_report_reasons` /
`admin_guild_reports.py::_summarize_report_reasons` -- both are private,
router-local helpers with no existing shared home in
`chat_report_service.py`/`guild_report_service.py` (confirmed via a real
read of both service files before writing this), and extracting them
into a new shared module purely for this task would touch two files
this task's own real scope does not otherwise need to change. Each copy
is a stable, three-line read of an unchanging table, unlikely to drift.
"""

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.chat import ChatMessage
from app.models.chat_moderation import REPORT_STATUS_MESSAGE_DELETED, ChatMessageReport, ChatMessageReportEntry
from app.models.dangerous_action_confirmation import (
    CONFIRMATION_STATUS_EXECUTED,
    CONFIRMATION_STATUS_PENDING,
    DangerousActionConfirmation,
)
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType, StudentVideoStatus
from app.models.guild_moderation import (
    REPORT_STATUS_CONTENT_DELETED,
    TARGET_TYPE_POST,
    GuildContentReport,
    GuildContentReportEntry,
)
from app.models.guilds import GuildComment, GuildPost
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import moderation_log_service, notification_service
from app.workers.cleanup_tasks import task_delete_orphaned_media

# STEP 1 INVESTIGATION result -- see `app/models/dangerous_action_confirmation.py`'s
# own module docstring for the full confirmation this is the COMPLETE
# real list (four, not three).
_ACTION_ITEM_TYPE: dict[ModerationAction, ModerationTargetType] = {
    ModerationAction.VIDEO_DELETE: ModerationTargetType.STUDENT_VIDEO,
    ModerationAction.VIDEO_REJECT_AND_PURGE: ModerationTargetType.STUDENT_VIDEO,
    ModerationAction.CHAT_MESSAGE_DELETE: ModerationTargetType.CHAT_MESSAGE_REPORT,
    ModerationAction.GUILD_CONTENT_DELETE: ModerationTargetType.GUILD_CONTENT_REPORT,
}

DANGEROUS_ACTIONS = frozenset(_ACTION_ITEM_TYPE)


class DangerousActionError(Exception):
    """Real, typed error this service raises -- callers (the four legacy
    routes and `admin_dangerous_actions.py`) translate `status_code`/
    `detail` into the real `HTTPException`, same "service raises typed
    errors, router translates them" split this codebase already uses
    elsewhere (e.g. `moderation_claim_service`'s own exception
    classes)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@dataclass
class _ExecutionResult:
    """What one `_execute_*` function hands back to `_finish_execution`
    -- the real underlying `ModerationLogEntry` it wrote, plus whatever
    POST-COMMIT side effects (R2 cleanup dispatch, student notification)
    the ORIGINAL inline handler body used to perform after its own
    `db.commit()`. Kept as plain data, not performed inline, so
    `approve_confirmation`/`override_confirmation` can commit ONE single
    transaction (the mutation + the DANGEROUS_ACTION_APPROVED/
    OVERRIDDEN log entry together) before any fire-and-forget dispatch or
    notification runs -- same ordering discipline every one of these
    four actions already had before this task.
    """

    log_entry_id: uuid.UUID
    r2_urls: list[str] = field(default_factory=list)
    notify_user_id: uuid.UUID | None = None
    notify_title: str = ""
    notify_body: str = ""
    notify_action: ModerationAction | None = None


async def _execute_video_delete(
    db: AsyncSession, *, actor: User, actor_role: AdminRole, item_id: uuid.UUID, notes: str | None
) -> _ExecutionResult:
    """MOVED from `admin_moderation.py::delete_video_permanently` --
    real mutation logic unchanged: captures R2 urls before the row
    delete, writes the SAME `VIDEO_DELETE` log entry shape, keeps R2
    dispatch/notification as post-commit side effects the caller
    performs (see `_ExecutionResult`'s own docstring)."""
    video = await db.get(StudentVideo, item_id)
    if video is None:
        raise DangerousActionError(404, "Student video not found")

    r2_urls = [url for url in (video.hls_master_url, video.thumbnail_url) if url]
    deleted_video_id = video.id
    deleted_student_id = video.student_id
    deleted_video_title = video.title

    await db.delete(video)

    log_entry = moderation_log_service.log_action(
        db,
        actor_id=actor.id,
        actor_role=actor_role,
        action=ModerationAction.VIDEO_DELETE,
        target_type=ModerationTargetType.STUDENT_VIDEO,
        target_id=deleted_video_id,
        outcome={
            "video_deleted": True,
            "student_id": str(deleted_student_id),
            "r2_urls_purged": r2_urls,
            "notes": notes,
        },
    )
    await db.flush()

    reason_text = notes or "No additional reason was provided by the reviewing moderator."
    return _ExecutionResult(
        log_entry_id=log_entry.id,
        r2_urls=r2_urls,
        notify_user_id=deleted_student_id,
        notify_title="Your video was permanently removed",
        notify_body=f'Your video "{deleted_video_title}" was permanently removed by an admin. Reason: {reason_text}',
        notify_action=ModerationAction.VIDEO_DELETE,
    )


async def _execute_video_reject_and_purge(
    db: AsyncSession, *, actor: User, actor_role: AdminRole, item_id: uuid.UUID, notes: str | None
) -> _ExecutionResult:
    """MOVED from `admin_moderation.py::reject_video_and_purge_media` --
    real mutation logic unchanged: keeps the row (REJECTED, with notes),
    purges only the media, clears the now-dangling R2 urls on the row."""
    video = await db.get(StudentVideo, item_id)
    if video is None:
        raise DangerousActionError(404, "Student video not found")

    r2_urls = [url for url in (video.hls_master_url, video.thumbnail_url) if url]

    video.status = StudentVideoStatus.REJECTED.value
    video.admin_override = True
    video.admin_notes = notes
    video.hls_master_url = None
    video.thumbnail_url = None
    db.add(video)

    log_entry = moderation_log_service.log_action(
        db,
        actor_id=actor.id,
        actor_role=actor_role,
        action=ModerationAction.VIDEO_REJECT_AND_PURGE,
        target_type=ModerationTargetType.STUDENT_VIDEO,
        target_id=video.id,
        outcome={"new_status": video.status, "r2_urls_purged": r2_urls},
    )
    await db.flush()

    reason_text = notes or "No additional reason was provided by the reviewing moderator."
    return _ExecutionResult(
        log_entry_id=log_entry.id,
        r2_urls=r2_urls,
        notify_user_id=video.student_id,
        notify_title="Your video's media was permanently removed",
        notify_body=f'Your video "{video.title}" was rejected and its media was permanently removed. Reason: {reason_text}',
        notify_action=ModerationAction.VIDEO_REJECT_AND_PURGE,
    )


async def _summarize_chat_report_reasons(db: AsyncSession, report_id: uuid.UUID) -> str:
    result = await db.execute(
        select(ChatMessageReportEntry.reason).where(ChatMessageReportEntry.report_id == report_id).distinct()
    )
    reasons = [r.value.replace("_", " ") for r in result.scalars().all()]
    return ", ".join(reasons) if reasons else "a policy violation"


async def _execute_chat_message_delete(
    db: AsyncSession, *, actor: User, actor_role: AdminRole, item_id: uuid.UUID, notes: str | None
) -> _ExecutionResult:
    """MOVED from `admin_chat_reports.py::delete_reported_message` --
    real mutation logic unchanged, including the disclosed
    resolve-before-delete cascade ordering."""
    report = await db.get(ChatMessageReport, item_id)
    if report is None:
        raise DangerousActionError(404, "Chat message report not found")

    message = await db.get(ChatMessage, report.message_id)
    if message is None:
        raise DangerousActionError(404, "Reported message no longer exists")

    deleted_message_id = message.id
    deleted_sender_id = message.sender_id

    report.status = REPORT_STATUS_MESSAGE_DELETED
    report.resolved_by = actor.id
    db.add(report)

    log_entry = moderation_log_service.log_action(
        db,
        actor_id=actor.id,
        actor_role=actor_role,
        action=ModerationAction.CHAT_MESSAGE_DELETE,
        target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
        target_id=report.id,
        outcome={"message_id": str(deleted_message_id), "sender_id": str(deleted_sender_id), "notes": notes},
    )

    reason_summary = await _summarize_chat_report_reasons(db, item_id)

    await db.delete(message)
    await db.flush()

    extra = f" Additional note: {notes}" if notes else ""
    return _ExecutionResult(
        log_entry_id=log_entry.id,
        notify_user_id=deleted_sender_id,
        notify_title="Your message was removed",
        notify_body=f"A message you sent was removed after being reported for {reason_summary}.{extra}",
        notify_action=ModerationAction.CHAT_MESSAGE_DELETE,
    )


async def _summarize_guild_report_reasons(db: AsyncSession, report_id: uuid.UUID) -> str:
    result = await db.execute(
        select(GuildContentReportEntry.reason).where(GuildContentReportEntry.report_id == report_id).distinct()
    )
    reasons = [r.value.replace("_", " ") for r in result.scalars().all()]
    return ", ".join(reasons) if reasons else "a policy violation"


async def _execute_guild_content_delete(
    db: AsyncSession, *, actor: User, actor_role: AdminRole, item_id: uuid.UUID, notes: str | None
) -> _ExecutionResult:
    """MOVED from `admin_guild_reports.py::delete_reported_guild_content`
    -- real mutation logic unchanged, including the disclosed
    resolve-before-delete ordering."""
    report = await db.get(GuildContentReport, item_id)
    if report is None:
        raise DangerousActionError(404, "Guild content report not found")

    if report.target_type == TARGET_TYPE_POST:
        content_row = await db.get(GuildPost, report.target_id)
    else:
        content_row = await db.get(GuildComment, report.target_id)
    if content_row is None:
        raise DangerousActionError(404, "Reported content no longer exists")
    author_id = content_row.user_id

    report.status = REPORT_STATUS_CONTENT_DELETED
    report.resolved_by = actor.id
    db.add(report)

    log_entry = moderation_log_service.log_action(
        db,
        actor_id=actor.id,
        actor_role=actor_role,
        action=ModerationAction.GUILD_CONTENT_DELETE,
        target_type=ModerationTargetType.GUILD_CONTENT_REPORT,
        target_id=report.id,
        outcome={
            "content_target_type": report.target_type,
            "content_target_id": str(report.target_id),
            "author_id": str(author_id),
            "notes": notes,
        },
    )

    reason_summary = await _summarize_guild_report_reasons(db, item_id)
    content_label = "post" if report.target_type == TARGET_TYPE_POST else "comment"

    await db.delete(content_row)
    await db.flush()

    extra = f" Additional note: {notes}" if notes else ""
    return _ExecutionResult(
        log_entry_id=log_entry.id,
        notify_user_id=author_id,
        notify_title=f"Your guild {content_label} was removed",
        notify_body=f"A guild {content_label} you posted was removed after being reported for {reason_summary}.{extra}",
        notify_action=ModerationAction.GUILD_CONTENT_DELETE,
    )


_EXECUTORS = {
    ModerationAction.VIDEO_DELETE: _execute_video_delete,
    ModerationAction.VIDEO_REJECT_AND_PURGE: _execute_video_reject_and_purge,
    ModerationAction.CHAT_MESSAGE_DELETE: _execute_chat_message_delete,
    ModerationAction.GUILD_CONTENT_DELETE: _execute_guild_content_delete,
}


async def _finish_execution(
    db: AsyncSession,
    *,
    confirmation: DangerousActionConfirmation,
    executor: User,
    executor_role: AdminRole,
    is_override: bool,
) -> DangerousActionConfirmation:
    """Shared tail for `approve_confirmation`/`override_confirmation`:
    runs the real mutation via `_EXECUTORS[confirmation.action]`, writes
    the DISTINCT `DANGEROUS_ACTION_APPROVED`/`DANGEROUS_ACTION_OVERRIDDEN`
    log entry (never the same member for both -- see `app/models/
    dangerous_action_confirmation.py`'s own `is_override` docstring
    reasoning), marks the confirmation `CONFIRMATION_STATUS_EXECUTED`,
    commits ONE transaction, THEN performs the real post-commit R2
    dispatch / student notification -- same ordering every one of the
    four original inline handlers already used before this task.
    """
    run = _EXECUTORS[confirmation.action]
    result = await run(
        db, actor=executor, actor_role=executor_role, item_id=confirmation.item_id, notes=confirmation.notes
    )

    confirmation.status = CONFIRMATION_STATUS_EXECUTED
    confirmation.is_override = is_override
    confirmation.executed_by = executor.id
    confirmation.executed_at = dt.datetime.now(dt.timezone.utc)
    db.add(confirmation)

    moderation_log_service.log_action(
        db,
        actor_id=executor.id,
        actor_role=executor_role,
        action=ModerationAction.DANGEROUS_ACTION_OVERRIDDEN if is_override else ModerationAction.DANGEROUS_ACTION_APPROVED,
        target_type=confirmation.item_type,
        target_id=confirmation.item_id,
        outcome={
            "confirmation_id": str(confirmation.id),
            "underlying_action": confirmation.action.value,
            "underlying_log_entry_id": str(result.log_entry_id),
            "requested_by": str(confirmation.requested_by) if confirmation.requested_by else None,
        },
    )

    await db.commit()

    if result.r2_urls:
        task_delete_orphaned_media.delay(r2_urls=result.r2_urls)

    if result.notify_user_id is not None:
        try:
            await notification_service.create_notification(
                db,
                result.notify_user_id,
                "WARNING",
                result.notify_title,
                result.notify_body,
                payload={
                    "type": "moderation_action",
                    "log_entry_id": str(result.log_entry_id),
                    "action": result.notify_action.value if result.notify_action else None,
                    "appealable": True,
                },
            )
        except Exception:
            # Defensive, matching the ORIGINAL inline handlers'
            # "notification is best-effort, never breaks the already-
            # committed real action" posture -- but UNLIKE those (whose
            # own notification attempt was always their last statement
            # before `return`), the caller here (`admin_dangerous_
            # actions.py::_to_out`) still reads attributes off
            # `confirmation` AFTER this returns. A failed INSERT leaves
            # the session's transaction in a rolled-back state that
            # would break that later read -- `db.rollback()` clears
            # ONLY the failed, uncommitted notification attempt (the
            # real mutation above is already safely committed), but
            # `rollback()` also unconditionally EXPIRES every object in
            # the session regardless of this session's own
            # `expire_on_commit=False` posture (`app/db/session.py`),
            # so a later plain attribute read on `confirmation` would
            # try a sync lazy-load and raise `MissingGreenlet`. A real
            # `db.refresh(confirmation)` right after the rollback
            # reloads it the safe async way, so the caller's later
            # attribute reads are real, already-loaded values again.
            await db.rollback()
            await db.refresh(confirmation)

    return confirmation


async def get_active_pending(
    db: AsyncSession, *, action: ModerationAction, item_type: ModerationTargetType, item_id: uuid.UUID
) -> DangerousActionConfirmation | None:
    result = await db.execute(
        select(DangerousActionConfirmation)
        .where(
            DangerousActionConfirmation.action == action,
            DangerousActionConfirmation.item_type == item_type,
            DangerousActionConfirmation.item_id == item_id,
            DangerousActionConfirmation.status == CONFIRMATION_STATUS_PENDING,
        )
        .order_by(DangerousActionConfirmation.requested_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _validate_action_item_type(action: ModerationAction, item_type: ModerationTargetType) -> None:
    if action not in DANGEROUS_ACTIONS:
        raise DangerousActionError(400, f"'{action.value}' is not a real dangerous action requiring confirmation.")
    if _ACTION_ITEM_TYPE[action] != item_type:
        raise DangerousActionError(
            400, f"'{action.value}' applies to '{_ACTION_ITEM_TYPE[action].value}' items, not '{item_type.value}'."
        )


async def request_confirmation(
    db: AsyncSession,
    *,
    action: ModerationAction,
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    notes: str | None,
    requester: User,
    requester_role: AdminRole,
) -> tuple[DangerousActionConfirmation, bool]:
    """Real, idempotent "request" step -- returns `(confirmation, created)`.
    Re-calling this for the SAME still-pending `(action, item_type,
    item_id)` returns the EXISTING row unchanged (same idempotent-not-
    error posture `moderation_escalation_service.raise_escalation`
    already established), never a second, competing pending request."""
    _validate_action_item_type(action, item_type)

    existing = await get_active_pending(db, action=action, item_type=item_type, item_id=item_id)
    if existing is not None:
        return existing, False

    confirmation = DangerousActionConfirmation(
        action=action, item_type=item_type, item_id=item_id, notes=notes,
        status=CONFIRMATION_STATUS_PENDING, requested_by=requester.id,
    )
    db.add(confirmation)
    await db.flush()

    moderation_log_service.log_action(
        db,
        actor_id=requester.id,
        actor_role=requester_role,
        action=ModerationAction.DANGEROUS_ACTION_REQUESTED,
        target_type=item_type,
        target_id=item_id,
        outcome={"confirmation_id": str(confirmation.id), "action": action.value, "notes": notes},
    )
    return confirmation, True


async def approve_confirmation(
    db: AsyncSession, *, confirmation: DangerousActionConfirmation, approver: User, approver_role: AdminRole
) -> DangerousActionConfirmation:
    """Real second-approver step. REJECTS SELF-APPROVAL (this task's own
    required central rule): the requester cannot also be the approver --
    a real two-person control, not a single Senior Moderator clicking
    twice."""
    if confirmation.status != CONFIRMATION_STATUS_PENDING:
        raise DangerousActionError(409, f"This confirmation is already '{confirmation.status}', not pending.")
    if confirmation.requested_by is not None and confirmation.requested_by == approver.id:
        raise DangerousActionError(
            403, "The person who requested this action cannot also approve it -- a real second approver is required."
        )
    return await _finish_execution(
        db, confirmation=confirmation, executor=approver, executor_role=approver_role, is_override=False
    )


async def override_confirmation(
    db: AsyncSession,
    *,
    action: ModerationAction,
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    notes: str | None,
    owner: User,
    owner_role: AdminRole,
) -> DangerousActionConfirmation:
    """Real Owner-override path -- see `app/models/
    dangerous_action_confirmation.py`'s own `is_override` docstring for
    the full design reasoning (a deliberate, distinct, always-visible
    act, never a silent bypass of `approve_confirmation`). Reuses an
    existing PENDING confirmation for this `(action, item_type, item_id)`
    if one exists (so a request a second person never got to still ends
    up correctly attributed to its real original requester); otherwise
    creates one fresh, with the Owner as their own requester -- the
    genuinely no-one-else-was-asked emergency case this feature exists
    to not block."""
    _validate_action_item_type(action, item_type)

    confirmation = await get_active_pending(db, action=action, item_type=item_type, item_id=item_id)
    if confirmation is None:
        confirmation = DangerousActionConfirmation(
            action=action, item_type=item_type, item_id=item_id, notes=notes,
            status=CONFIRMATION_STATUS_PENDING, requested_by=owner.id,
        )
        db.add(confirmation)
        await db.flush()
        moderation_log_service.log_action(
            db,
            actor_id=owner.id,
            actor_role=owner_role,
            action=ModerationAction.DANGEROUS_ACTION_REQUESTED,
            target_type=item_type,
            target_id=item_id,
            outcome={
                "confirmation_id": str(confirmation.id), "action": action.value, "notes": notes,
                "self_requested_for_override": True,
            },
        )

    return await _finish_execution(
        db, confirmation=confirmation, executor=owner, executor_role=owner_role, is_override=True
    )


async def list_pending(db: AsyncSession, *, limit: int, offset: int) -> tuple[list[tuple], int]:
    """`GET /admin/moderation/dangerous-actions`'s own whole-team query
    -- every currently-pending confirmation, oldest first (the
    longest-waiting request is the one most likely to need attention)."""
    total = (
        await db.execute(
            select(sa_func.count())
            .select_from(DangerousActionConfirmation)
            .where(DangerousActionConfirmation.status == CONFIRMATION_STATUS_PENDING)
        )
    ).scalar_one()

    Requester = aliased(User)
    result = await db.execute(
        select(DangerousActionConfirmation, Requester.full_name)
        .outerjoin(Requester, Requester.id == DangerousActionConfirmation.requested_by)
        .where(DangerousActionConfirmation.status == CONFIRMATION_STATUS_PENDING)
        .order_by(DangerousActionConfirmation.requested_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return [(row[0], row[1]) for row in result.all()], total
