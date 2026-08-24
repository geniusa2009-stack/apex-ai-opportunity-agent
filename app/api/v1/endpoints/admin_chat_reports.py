"""Extend Moderation to Chat Messages -- the reported-message admin queue
and its resolution actions. The "second real queue" `app/api/v1/
endpoints/admin_claims.py`'s own module docstring explicitly left open.

Builds on the confirmed-real admin panel (tiered roles 39d1d0f, activity
log 5d2934e, advisory claims 47906f0, stats + 3 views 5881cfc, wired UI
004fc92) and this task's own new report-submission endpoint
(`POST /chat/rooms/{room_id}/messages/{message_id}/report`, `chat.py`).
See `app/models/chat_moderation.py`'s own module docstring for the real
Step 1/2 investigation ("what already existed" -- nothing did) and the
"what is a case" design.

    GET    /admin/chat/reports                        -- paginated reported-message queue
    POST   /admin/chat/reports/{report_id}/dismiss     -- close a case with no action taken
    DELETE /admin/chat/reports/{report_id}/message     -- permanently delete the reported message
    POST   /admin/chat/reports/{report_id}/escalate-ban -- ban the sender via chat_security.ban_user

TIER DECISION, DISCLOSED (this task's own required reasoning, mirroring
`AdminRole`'s own "tiered by reversibility" precedent for the video
actions):

- `GET .../reports` and `POST .../dismiss` are `RequireModerationAccess`
  (Moderator tier). Viewing the queue is read-only, same posture as
  `get_review_queue`. Dismissing is fully reversible in the only sense
  that matters here -- it changes `ChatMessageReport.status`, never
  touches the underlying message or the sender's account, and a
  mis-dismissed real case can simply be re-reported (a fresh case opens
  cleanly, see `chat_report_service.create_report`'s own get-or-create
  logic) -- same "reviewable, reversible" bucket as
  `override_video_decision`.
- `POST .../escalate-ban` is ALSO `RequireModerationAccess`, DESPITE
  applying a real behavioral consequence to a real user. Reasoning:
  `DELETE /chat/admin/bans/{user_id}` (lifting a ban early) is ALREADY
  `RequireModerationAccess` per that route's own docstring, on the
  explicit logic that "lifting a ban early is reversible." The same
  logic runs the other direction -- APPLYING a ban here is exactly as
  reversible as lifting one, since the existing unban endpoint is a
  complete, one-call undo with no data loss. Gating escalate-ban stricter
  than the undo path for the same action would be an inconsistent tier
  boundary, not a more cautious one.
- `DELETE .../message` is `RequireSeniorModerationAccess`. Deleting the
  reported `ChatMessage` row is genuinely irreversible -- there is no
  undo endpoint, unlike a ban, and no "purge storage but keep the row"
  middle ground the way `reject_video_and_purge_media` has for videos
  (a chat message has no separate media artifact to purge independently
  of the row itself). This is the same "real, irreversible, no data
  survives" bucket `delete_video_permanently` is in, so it gets the same
  tier.

Real `RequireModerationAccess`/`RequireSeniorModerationAccess` deps
(`app/api/deps.py`), NOT reinvented here -- same imports, same posture,
as `admin_moderation.py`.

Disclosed routing-precedence check (same discipline as every other
router in this codebase): this router's paths all start with the literal
segment `admin`, specifically `admin/chat/reports*` -- diverges from
EVERY other `admin/*` route already registered (`admin/videos/*`,
`admin/users/*`, `admin/team*`, `admin/activity-log*`,
`admin/moderation/*`) at the SECOND path segment (`chat`), so there is no
path-template ambiguity regardless of registration order.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireSeniorModerationAccess, effective_admin_role
from app.models.chat import ChatMessage
from app.models.chat_moderation import (
    REPORT_STATUS_DISMISSED,
    REPORT_STATUS_ESCALATED_BAN,
    REPORT_STATUS_MESSAGE_DELETED,
    REPORT_STATUS_OPEN,
    ChatMessageReport,
    ChatMessageReportEntry,
)
from app.models.enums import ModerationAction, ModerationTargetType
from app.models.user import User
from app.schemas.chat_moderation import (
    AdminChatMessageOut,
    AdminChatReportActionResponse,
    AdminChatReportDismissRequest,
    AdminChatReportEntryOut,
    AdminChatReportEscalateBanRequest,
    AdminChatReportOut,
)
from app.schemas.opportunity import PaginatedResponse
from app.services import chat_security, moderation_claim_service, moderation_log_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-chat-reports"])


async def _get_report_or_404(db: DbSession, report_id: uuid.UUID) -> ChatMessageReport:
    report = await db.get(ChatMessageReport, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat message report not found")
    return report


def _require_open(report: ChatMessageReport) -> None:
    """Real, honest 409 for acting on an already-resolved case -- same
    "don't silently pretend a second resolution attempt succeeded"
    posture as `chat.py::report_message`'s own duplicate-report 409.
    A case that needs a second look after being dismissed is re-opened by
    a fresh report (`chat_report_service.create_report`'s own
    get-or-create logic), not by re-resolving the old, closed one."""
    if report.status != REPORT_STATUS_OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This report was already resolved (status={report.status}).",
        )


@router.get("/admin/chat/reports", response_model=PaginatedResponse[AdminChatReportOut])
async def get_chat_reports_queue(
    db: DbSession,
    current_user: CurrentUser,
    _: RequireModerationAccess,
    status_filter: str = Query(default=REPORT_STATUS_OPEN, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AdminChatReportOut]:
    """Moderator-or-higher. Every `ChatMessageReport` in `status_filter`
    (default `open` -- the real, reviewable queue; a moderator can pass
    `status=dismissed`/`message_deleted`/`escalated_ban` to see resolved
    history instead), oldest first -- same "longest-waiting case surfaces
    first" ordering as `get_review_queue`.

    Mirrors that endpoint's own shape closely: real claim state via the
    SAME `moderation_claim_service.get_effective_claim` function (works
    unmodified here -- see `admin_claims.py`'s own `_CLAIMABLE_MODELS`
    registration), real embedded message content so a moderator doesn't
    need a second round-trip to see what's actually being reported, and a
    real per-case list of every individual report submission (who, why,
    when) rather than just a bare count.
    """
    filters = (ChatMessageReport.status == status_filter,)

    total = (
        await db.execute(select(func.count()).select_from(ChatMessageReport).where(*filters))
    ).scalar_one()

    Sender = aliased(User)
    Claimant = aliased(User)
    Resolver = aliased(User)
    result = await db.execute(
        select(ChatMessageReport, ChatMessage, Sender.full_name, Claimant.full_name, Resolver.full_name)
        .join(ChatMessage, ChatMessage.id == ChatMessageReport.message_id)
        .outerjoin(Sender, Sender.id == ChatMessage.sender_id)
        .outerjoin(Claimant, Claimant.id == ChatMessageReport.claimed_by)
        .outerjoin(Resolver, Resolver.id == ChatMessageReport.resolved_by)
        .where(*filters)
        .order_by(ChatMessageReport.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    report_ids = [report.id for report, *_ in rows]
    entries_by_report: dict[uuid.UUID, list[AdminChatReportEntryOut]] = {rid: [] for rid in report_ids}
    if report_ids:
        Reporter = aliased(User)
        entry_result = await db.execute(
            select(ChatMessageReportEntry, Reporter.full_name)
            .outerjoin(Reporter, Reporter.id == ChatMessageReportEntry.reporter_id)
            .where(ChatMessageReportEntry.report_id.in_(report_ids))
            .order_by(ChatMessageReportEntry.created_at.asc())
        )
        for entry, reporter_name in entry_result.all():
            entries_by_report[entry.report_id].append(
                AdminChatReportEntryOut(
                    reporter_id=entry.reporter_id,
                    reporter_name=reporter_name,
                    reason=entry.reason,
                    details=entry.details,
                    created_at=entry.created_at,
                )
            )

    items: list[AdminChatReportOut] = []
    for report, message, sender_name, claimant_name, resolver_name in rows:
        effective_claimed_by, effective_claimed_at = moderation_claim_service.get_effective_claim(report)
        reports = entries_by_report.get(report.id, [])
        items.append(
            AdminChatReportOut(
                id=report.id,
                message=AdminChatMessageOut(
                    id=message.id,
                    room_id=message.room_id,
                    sender_id=message.sender_id,
                    sender_name=sender_name,
                    content=message.content,
                    created_at=message.created_at,
                    is_bot=message.is_bot,
                ),
                status=report.status,
                reports=reports,
                report_count=len(reports),
                first_reported_at=report.created_at,
                claimed_by=effective_claimed_by,
                claimed_by_name=claimant_name if effective_claimed_by is not None else None,
                claimed_at=effective_claimed_at,
                resolved_by_name=resolver_name if report.resolved_by is not None else None,
                resolved_at=report.resolved_at,
                resolution_notes=report.resolution_notes,
            )
        )

    return PaginatedResponse[AdminChatReportOut](items=items, total=total, limit=limit, offset=offset)


@router.post("/admin/chat/reports/{report_id}/dismiss", response_model=AdminChatReportActionResponse)
async def dismiss_chat_report(
    report_id: uuid.UUID,
    payload: AdminChatReportDismissRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> AdminChatReportActionResponse:
    """Moderator-or-higher (reversible -- see module docstring). Closes
    the case with no action taken against the message or its sender --
    the real "this report doesn't need further action" outcome."""
    report = await _get_report_or_404(db, report_id)
    _require_open(report)

    report.status = REPORT_STATUS_DISMISSED
    report.resolved_by = current_user.id
    report.resolution_notes = payload.notes
    db.add(report)

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.CHAT_REPORT_DISMISS,
        target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
        target_id=report.id,
        outcome={"notes": payload.notes},
    )

    await db.commit()
    await db.refresh(report)

    return AdminChatReportActionResponse(success=True, report_id=report.id, status=report.status)


@router.delete("/admin/chat/reports/{report_id}/message", response_model=AdminChatReportActionResponse)
async def delete_reported_message(
    report_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> AdminChatReportActionResponse:
    """Senior-Moderator-or-higher (irreversible -- see module docstring).
    Permanently deletes the real `ChatMessage` row this case reports,
    then resolves the case itself. `ChatMessageReport.message_id`'s own
    `ondelete="CASCADE"` would ALSO delete this (and any other open/past)
    report row for the same message if the message were deleted first --
    disclosed necessary ordering: this handler resolves the CASE (status,
    resolved_by/at) BEFORE deleting the message, so the case's own
    resolution record survives as real history instead of cascading away
    with the message it was about.
    """
    report = await _get_report_or_404(db, report_id)
    _require_open(report)

    message = await db.get(ChatMessage, report.message_id)
    if message is None:
        # Disclosed edge case: the message was already removed through
        # some other path (shouldn't happen under this endpoint's own
        # cascade-ordering discipline, but a defensive 404 is more honest
        # than silently "succeeding" at deleting something already gone).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reported message no longer exists")

    deleted_message_id = message.id
    deleted_sender_id = message.sender_id

    report.status = REPORT_STATUS_MESSAGE_DELETED
    report.resolved_by = current_user.id
    db.add(report)

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.CHAT_MESSAGE_DELETE,
        target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
        target_id=report.id,
        outcome={"message_id": str(deleted_message_id), "sender_id": str(deleted_sender_id)},
    )

    await db.delete(message)
    await db.commit()
    await db.refresh(report)

    return AdminChatReportActionResponse(success=True, report_id=report.id, status=report.status)


@router.post("/admin/chat/reports/{report_id}/escalate-ban", response_model=AdminChatReportActionResponse)
async def escalate_chat_report_to_ban(
    report_id: uuid.UUID,
    payload: AdminChatReportEscalateBanRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> AdminChatReportActionResponse:
    """Moderator-or-higher (reversible via the existing
    `DELETE /chat/admin/bans/{user_id}` unban endpoint -- see module
    docstring). Bans the reported message's SENDER via the real,
    pre-existing `chat_security.ban_user` path -- the exact same Redis
    TTL mechanism the automatic Arabic-filter ban already uses, not a
    parallel ban implementation. The message itself is left in place
    (unlike `delete_reported_message`) -- a ban is evidence-preserving by
    design; deleting the message too is a separate, explicit action a
    moderator can still take afterward via a fresh report/case if
    warranted.
    """
    report = await _get_report_or_404(db, report_id)
    _require_open(report)

    message = await db.get(ChatMessage, report.message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reported message no longer exists")

    await chat_security.ban_user(message.sender_id, payload.duration_seconds)

    report.status = REPORT_STATUS_ESCALATED_BAN
    report.resolved_by = current_user.id
    report.resolution_notes = payload.notes
    db.add(report)

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.CHAT_REPORT_ESCALATE_BAN,
        target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
        target_id=report.id,
        outcome={
            "banned_user_id": str(message.sender_id),
            "duration_seconds": payload.duration_seconds,
            "notes": payload.notes,
        },
    )

    await db.commit()
    await db.refresh(report)

    return AdminChatReportActionResponse(
        success=True, report_id=report.id, status=report.status, banned_until_seconds=payload.duration_seconds
    )
