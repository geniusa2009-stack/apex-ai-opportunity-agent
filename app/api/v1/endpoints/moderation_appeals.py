"""Transparency: Notify Users of Moderation Actions + Real Appeal Path --
the real appeal submission (student-facing) and review/resolution
(Senior-Moderator+) endpoints.

    POST   /moderation/appeals                  -- student submits a real appeal
    GET    /moderation/appeals/mine             -- student's own appeal history
    GET    /admin/appeals                       -- real appeal queue
    POST   /admin/appeals/{appeal_id}/resolve   -- uphold/deny + real reversal

TIER DECISION, DISCLOSED (this task's own required Step 3.2 reasoning):
`GET /admin/appeals` and `POST .../resolve` are BOTH
`RequireSeniorModerationAccess`, deliberately NOT the entry `Moderator`
tier the video/chat-report queues use. Reasoning: an appeal can reverse
an action taken by ANY tier, including a Senior Moderator's own
irreversible call (`VIDEO_DELETE`, `VIDEO_REJECT_AND_PURGE`,
`CHAT_MESSAGE_DELETE` are already Senior-Moderator-gated at the point
they're taken -- see `admin_moderation.py`/`admin_chat_reports.py`'s own
tier docstrings). Letting a plain Moderator overrule a Senior Moderator's
own irreversible decision would invert this codebase's own established
"more consequential power requires more standing" ordering. Unlike
`GET /admin/flagged-users` (Owner-only, a platform-wide roster with no
routine per-case workflow), appeal review IS a routine, recurring
workflow that would be too slow to route through the Owner alone at any
real scale -- so Senior Moderator, not Owner, is the real floor here:
strictly above the entry tier (protects against a Moderator overruling a
Senior Moderator), but not so high it bottlenecks on the one Owner.

Claim System reuse, per this task's own Step 3.2 question ("should
appeals be claimable"): YES -- `POST`/`DELETE /admin/moderation/
moderation_appeal/{appeal_id}/claim` already works with ZERO new code,
via `admin_claims.py`'s own `_CLAIMABLE_MODELS` dict (one new entry,
this task's own real registration). Claim/release itself stays gated at
the SAME `RequireModerationAccess` (Moderator) tier every other claimable
item uses -- claiming is advisory-only visibility, not the actual
resolve authority, so widening who can SEE "someone's on this" costs
nothing even though only a Senior Moderator can actually resolve it.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.deps import CurrentUser, DbSession, RequireSeniorModerationAccess, effective_admin_role
from app.models.moderation_appeal import APPEAL_STATUS_OPEN, ModerationAppeal
from app.models.moderation_log import ModerationLogEntry
from app.models.user import User
from app.schemas.moderation_appeal import (
    AppealOut,
    AppealResolveRequest,
    AppealResolveResponse,
    AppealSubmitRequest,
)
from app.schemas.opportunity import PaginatedResponse
from app.services import moderation_appeal_service, moderation_claim_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["moderation-appeals"])


async def _get_log_entry_or_404(db: DbSession, log_entry_id: uuid.UUID) -> ModerationLogEntry:
    entry = await db.get(ModerationLogEntry, log_entry_id)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Moderation action not found")
    return entry


async def _get_appeal_or_404(db: DbSession, appeal_id: uuid.UUID) -> ModerationAppeal:
    appeal = await db.get(ModerationAppeal, appeal_id)
    if appeal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appeal not found")
    return appeal


def _build_appeal_out(
    appeal: ModerationAppeal,
    entry: ModerationLogEntry,
    student_name: str | None,
    claimant_name: str | None = None,
    resolver_name: str | None = None,
) -> AppealOut:
    effective_claimed_by, effective_claimed_at = moderation_claim_service.get_effective_claim(appeal)
    return AppealOut(
        id=appeal.id,
        log_entry_id=appeal.log_entry_id,
        action=entry.action,
        target_type=entry.target_type,
        student_id=appeal.student_id,
        student_name=student_name,
        message=appeal.message,
        additional_context=appeal.additional_context,
        status=appeal.status,
        can_be_restored=appeal.can_be_restored,
        claimed_by=effective_claimed_by,
        claimed_by_name=claimant_name if effective_claimed_by is not None else None,
        claimed_at=effective_claimed_at,
        resolved_by_name=resolver_name if appeal.resolved_by is not None else None,
        resolved_at=appeal.resolved_at,
        resolution_notes=appeal.resolution_notes,
        created_at=appeal.created_at,
    )


# ---------------------------------------------------------------------
# Student-facing
# ---------------------------------------------------------------------


@router.post("/moderation/appeals", response_model=AppealOut, status_code=status.HTTP_201_CREATED)
async def submit_appeal(
    payload: AppealSubmitRequest, current_user: CurrentUser, db: DbSession
) -> AppealOut:
    """Any authenticated user may call this -- the real gate is inside
    `moderation_appeal_service.create_appeal`, which 403s (via
    `NotTheAffectedUserError`) unless the caller IS the real, resolved
    offender for the referenced log entry. Same "endpoint itself is
    open, the service layer enforces the real ownership rule" posture as
    `chat.py::report_message`'s own self-report check.
    """
    entry = await _get_log_entry_or_404(db, payload.log_entry_id)

    try:
        appeal = await moderation_appeal_service.create_appeal(
            db,
            log_entry=entry,
            student_id=current_user.id,
            message=payload.message,
            additional_context=payload.additional_context,
        )
    except moderation_appeal_service.AppealNotAllowedError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This moderation action cannot be appealed.",
        )
    except moderation_appeal_service.NotTheAffectedUserError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You may only appeal a moderation action taken against you.",
        )
    except moderation_appeal_service.DuplicateOpenAppealError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an open appeal for this action.",
        )

    return _build_appeal_out(appeal, entry, current_user.full_name)


@router.get("/moderation/appeals/mine", response_model=PaginatedResponse[AppealOut])
async def list_my_appeals(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AppealOut]:
    """A student's own appeal history -- so submitting one isn't a
    "shout into the void" action; they can come back and see its real
    status/resolution."""
    filters = (ModerationAppeal.student_id == current_user.id,)

    total = (
        await db.execute(select(func.count()).select_from(ModerationAppeal).where(*filters))
    ).scalar_one()

    Resolver = aliased(User)
    result = await db.execute(
        select(ModerationAppeal, ModerationLogEntry, Resolver.full_name)
        .join(ModerationLogEntry, ModerationLogEntry.id == ModerationAppeal.log_entry_id)
        .outerjoin(Resolver, Resolver.id == ModerationAppeal.resolved_by)
        .where(*filters)
        .order_by(ModerationAppeal.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        _build_appeal_out(appeal, entry, current_user.full_name, resolver_name=resolver_name)
        for appeal, entry, resolver_name in result.all()
    ]
    return PaginatedResponse[AppealOut](items=items, total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------
# Senior-Moderator+ facing
# ---------------------------------------------------------------------


@router.get("/admin/appeals", response_model=PaginatedResponse[AppealOut])
async def get_appeals_queue(
    db: DbSession,
    _: RequireSeniorModerationAccess,
    status_filter: str = Query(default=APPEAL_STATUS_OPEN, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AppealOut]:
    """Senior-Moderator-or-higher (see module docstring). Same "oldest
    real open case surfaces first" ordering as every other real queue in
    this codebase."""
    filters = (ModerationAppeal.status == status_filter,)

    total = (
        await db.execute(select(func.count()).select_from(ModerationAppeal).where(*filters))
    ).scalar_one()

    Student = aliased(User)
    Claimant = aliased(User)
    Resolver = aliased(User)
    result = await db.execute(
        select(ModerationAppeal, ModerationLogEntry, Student.full_name, Claimant.full_name, Resolver.full_name)
        .join(ModerationLogEntry, ModerationLogEntry.id == ModerationAppeal.log_entry_id)
        .outerjoin(Student, Student.id == ModerationAppeal.student_id)
        .outerjoin(Claimant, Claimant.id == ModerationAppeal.claimed_by)
        .outerjoin(Resolver, Resolver.id == ModerationAppeal.resolved_by)
        .where(*filters)
        .order_by(ModerationAppeal.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        _build_appeal_out(appeal, entry, student_name, claimant_name, resolver_name)
        for appeal, entry, student_name, claimant_name, resolver_name in result.all()
    ]
    return PaginatedResponse[AppealOut](items=items, total=total, limit=limit, offset=offset)


@router.post("/admin/appeals/{appeal_id}/resolve", response_model=AppealResolveResponse)
async def resolve_appeal_route(
    appeal_id: uuid.UUID,
    payload: AppealResolveRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> AppealResolveResponse:
    """Senior-Moderator-or-higher (see module docstring). Real, honest
    409 for re-resolving an already-resolved appeal -- same "don't
    silently pretend a second resolution attempt succeeded" posture as
    `admin_chat_reports.py::_require_open`."""
    appeal = await _get_appeal_or_404(db, appeal_id)
    if appeal.status != APPEAL_STATUS_OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This appeal was already resolved (status={appeal.status}).",
        )

    entry = await _get_log_entry_or_404(db, appeal.log_entry_id)

    _, reversal_applied, reversal_detail = await moderation_appeal_service.resolve_appeal(
        db,
        appeal=appeal,
        log_entry=entry,
        decision=payload.decision,
        resolver_id=current_user.id,
        resolver_role=effective_admin_role(current_user),
        notes=payload.notes,
    )

    return AppealResolveResponse(
        success=True,
        appeal_id=appeal.id,
        status=appeal.status,
        reversal_applied=reversal_applied,
        reversal_detail=reversal_detail,
    )
