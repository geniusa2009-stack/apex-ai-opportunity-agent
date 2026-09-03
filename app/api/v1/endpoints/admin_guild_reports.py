"""Review of Guild Content -- the guild-content report queue and its
resolution actions.

See `app/models/guild_moderation.py`'s own module docstring for the full
Step 1/2 investigation: the Gemini safety/PII check and the general
blacklist ALREADY cover guild content automatically and (for the
blacklist half) are already visible via the existing Blacklist tab --
the one genuine, confirmed gap this closes is that no user could report
an already-published post/comment.

    GET    /admin/guild/reports                     -- paginated report queue
    POST   /admin/guild/reports/{report_id}/dismiss  -- close a case with no action taken
    DELETE /admin/guild/reports/{report_id}/content  -- permanently delete the reported post/comment

TIER DECISION, DISCLOSED -- same reasoning `admin_chat_reports.py`
already established for its own near-identical shape:

- `GET .../reports` and `POST .../dismiss` are `RequireModerationAccess`
  (Moderator tier) -- read-only / fully reversible (a mis-dismissed case
  simply re-opens on a fresh report, same get-or-create posture
  `guild_report_service.create_report` already has).
- `DELETE .../content` is `RequireSeniorModerationAccess` -- genuinely
  irreversible (no undo endpoint, the row is simply gone), same bucket
  `DELETE /admin/chat/reports/{report_id}/message` is in.

DELIBERATELY NOT BUILT, DISCLOSED: an "escalate to ban" action.
`chat_message_report`'s own escalate-ban exists because THAT queue's
resolution needs a real behavioral consequence against the sender with
no other real path to one from inside that same review flow. Here, a
moderator who decides a report's author needs banning already has a
real, general-purpose tool one click away: this queue embeds the
content's real `author_id`/`author_name` (see `AdminGuildContentOut`),
and `POST /chat/admin/bans/{user_id}` (Manual Timed Bans phase) already
bans ANY real user by id, not just one who has sent a chat message.
Building a second, guild-specific escalate-ban here would duplicate that
existing, already-tiered, already-tested action rather than reuse it --
so this queue deliberately stops at dismiss/delete, and a moderator who
needs to ban reaches the existing ban control directly.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy.orm import aliased

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireSeniorModerationAccess, effective_admin_role
from app.models.enums import ModerationAction, ModerationTargetType
from app.models.guild_moderation import (
    REPORT_STATUS_CONTENT_DELETED,
    REPORT_STATUS_DISMISSED,
    REPORT_STATUS_OPEN,
    TARGET_TYPE_COMMENT,
    TARGET_TYPE_POST,
    GuildContentReport,
    GuildContentReportEntry,
)
from app.models.guilds import GuildComment, GuildPost
from app.models.user import User
from app.schemas.guild_moderation import (
    AdminGuildContentOut,
    AdminGuildReportActionResponse,
    AdminGuildReportDeleteContentRequest,
    AdminGuildReportDismissRequest,
    AdminGuildReportEntryOut,
    AdminGuildReportOut,
)
from app.schemas.dangerous_actions import DangerousActionPendingResponse
from app.schemas.opportunity import PaginatedResponse
from app.services import (
    dangerous_action_service,
    moderation_claim_service,
    moderation_log_service,
    notification_service,
    repeat_offender_service,
)
from sqlalchemy import func, select

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-guild-reports"])


async def _get_report_or_404(db: DbSession, report_id: uuid.UUID) -> GuildContentReport:
    report = await db.get(GuildContentReport, report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guild content report not found")
    return report


def _require_open(report: GuildContentReport) -> None:
    if report.status != REPORT_STATUS_OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This report was already resolved (status={report.status}).",
        )


async def _summarize_report_reasons(db: DbSession, report_id: uuid.UUID) -> str:
    result = await db.execute(
        select(GuildContentReportEntry.reason).where(GuildContentReportEntry.report_id == report_id).distinct()
    )
    reasons = [r.value.replace("_", " ") for r in result.scalars().all()]
    return ", ".join(reasons) if reasons else "a policy violation"


async def _load_content(db: DbSession, target_type: str, target_id: uuid.UUID) -> tuple[AdminGuildContentOut | None, uuid.UUID | None]:
    """Real embedded content for a queue item, resolved by `target_type`
    -- returns `(content_out, author_id)`; `content_out` is `None` (a
    real, disclosed edge case, not an error) if the post/comment was
    already removed through some other path since the report was filed.
    """
    Author = aliased(User)
    if target_type == TARGET_TYPE_POST:
        row = (
            await db.execute(
                select(GuildPost, Author.full_name).outerjoin(Author, Author.id == GuildPost.user_id).where(GuildPost.id == target_id)
            )
        ).first()
        if row is None:
            return None, None
        post, author_name = row
        return (
            AdminGuildContentOut(id=post.id, author_id=post.user_id, author_name=author_name, content=post.content, created_at=post.created_at),
            post.user_id,
        )
    else:
        row = (
            await db.execute(
                select(GuildComment, Author.full_name).outerjoin(Author, Author.id == GuildComment.user_id).where(GuildComment.id == target_id)
            )
        ).first()
        if row is None:
            return None, None
        comment, author_name = row
        return (
            AdminGuildContentOut(id=comment.id, author_id=comment.user_id, author_name=author_name, content=comment.content, created_at=comment.created_at),
            comment.user_id,
        )


@router.get("/admin/guild/reports", response_model=PaginatedResponse[AdminGuildReportOut])
async def get_guild_reports_queue(
    db: DbSession,
    current_user: CurrentUser,
    _: RequireModerationAccess,
    status_filter: str = Query(default=REPORT_STATUS_OPEN, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AdminGuildReportOut]:
    """Moderator-or-higher. Mirrors `get_chat_reports_queue`'s own shape
    closely: real claim state, real embedded content, a real per-case
    list of every individual report submission.
    """
    filters = (GuildContentReport.status == status_filter,)

    total = (
        await db.execute(select(func.count()).select_from(GuildContentReport).where(*filters))
    ).scalar_one()

    Claimant = aliased(User)
    Resolver = aliased(User)
    result = await db.execute(
        select(GuildContentReport, Claimant.full_name, Resolver.full_name)
        .outerjoin(Claimant, Claimant.id == GuildContentReport.claimed_by)
        .outerjoin(Resolver, Resolver.id == GuildContentReport.resolved_by)
        .where(*filters)
        .order_by(GuildContentReport.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    report_ids = [report.id for report, *_ in rows]
    entries_by_report: dict[uuid.UUID, list[AdminGuildReportEntryOut]] = {rid: [] for rid in report_ids}
    if report_ids:
        Reporter = aliased(User)
        entry_result = await db.execute(
            select(GuildContentReportEntry, Reporter.full_name)
            .outerjoin(Reporter, Reporter.id == GuildContentReportEntry.reporter_id)
            .where(GuildContentReportEntry.report_id.in_(report_ids))
            .order_by(GuildContentReportEntry.created_at.asc())
        )
        for entry, reporter_name in entry_result.all():
            entries_by_report[entry.report_id].append(
                AdminGuildReportEntryOut(
                    reporter_id=entry.reporter_id, reporter_name=reporter_name,
                    reason=entry.reason, details=entry.details, created_at=entry.created_at,
                )
            )

    offense_counts = await repeat_offender_service.get_recent_offense_counts(db)

    items: list[AdminGuildReportOut] = []
    for report, claimant_name, resolver_name in rows:
        effective_claimed_by, effective_claimed_at = moderation_claim_service.get_effective_claim(report)
        content_out, author_id = await _load_content(db, report.target_type, report.target_id)
        reports = entries_by_report.get(report.id, [])
        items.append(
            AdminGuildReportOut(
                id=report.id,
                target_type=report.target_type,
                target_id=report.target_id,
                content=content_out,
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
                offender_recent_action_count=offense_counts.get(author_id, 0) if author_id else 0,
            )
        )

    return PaginatedResponse[AdminGuildReportOut](items=items, total=total, limit=limit, offset=offset)


@router.post("/admin/guild/reports/{report_id}/dismiss", response_model=AdminGuildReportActionResponse)
async def dismiss_guild_report(
    report_id: uuid.UUID,
    payload: AdminGuildReportDismissRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> AdminGuildReportActionResponse:
    """Moderator-or-higher (reversible -- see module docstring)."""
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
        action=ModerationAction.GUILD_REPORT_DISMISS,
        target_type=ModerationTargetType.GUILD_CONTENT_REPORT,
        target_id=report.id,
        outcome={"notes": payload.notes},
    )

    await db.commit()
    await db.refresh(report)

    return AdminGuildReportActionResponse(success=True, report_id=report.id, status=report.status)


@router.delete("/admin/guild/reports/{report_id}/content", response_model=DangerousActionPendingResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_reported_guild_content(
    report_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
    payload: AdminGuildReportDeleteContentRequest | None = None,
) -> DangerousActionPendingResponse:
    """Senior-Moderator-or-higher. Real Two-Person Confirmation for
    Dangerous Actions addition (migration 0054) -- THIS ROUTE NO LONGER
    DELETES THE CONTENT DIRECTLY. See `app/models/
    dangerous_action_confirmation.py`'s own module docstring, STEP 3,
    for the full "why". Calling this route creates (or, if one is
    already pending, idempotently returns) a real
    `DangerousActionConfirmation` row -- the post/comment is NOT deleted
    by this call alone; a real second Senior-Moderator-or-Owner approval
    or a deliberate Owner override is required. The real deletion logic
    itself (cascade-safe resolve-before-delete ordering,
    `ModerationLogEntry`, notification) is UNCHANGED, just moved to
    `dangerous_action_service._execute_guild_content_delete`.
    """
    report = await _get_report_or_404(db, report_id)
    _require_open(report)

    notes = payload.notes if payload is not None else None
    confirmation, _created = await dangerous_action_service.request_confirmation(
        db,
        action=ModerationAction.GUILD_CONTENT_DELETE,
        item_type=ModerationTargetType.GUILD_CONTENT_REPORT,
        item_id=report_id,
        notes=notes,
        requester=current_user,
        requester_role=effective_admin_role(current_user),
    )
    await db.commit()
    await db.refresh(confirmation)
    return DangerousActionPendingResponse(
        confirmation_id=confirmation.id,
        action=confirmation.action,
        item_type=confirmation.item_type,
        item_id=confirmation.item_id,
        status=confirmation.status,
        requested_by=confirmation.requested_by,
        requested_at=confirmation.requested_at,
    )
