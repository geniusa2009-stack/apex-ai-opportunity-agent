"""Volunteer Coverage / Shift Schedule -- endpoints.

    POST   /admin/shifts                (Owner) -- create a real shift
    PATCH  /admin/shifts/{shift_id}     (Owner) -- edit a real shift
    DELETE /admin/shifts/{shift_id}     (Owner) -- delete a real shift
    GET    /admin/shifts                (Moderator+) -- the real team-wide
                                          schedule (see this feature's own
                                          visibility-tier decision below)
    GET    /admin/shifts/export.ics     (Moderator+) -- real RFC 5545
                                          calendar export

VISIBILITY-TIER DECISION: write access is Owner-only (creating/editing/
deleting who covers what is a coordination/management action the Owner
makes, exactly the same posture `PATCH /admin/users/{id}/role` already
established for role management -- this is a DIFFERENT kind of "manage
the team" action from that one, but the same actor). READ access to the
full team schedule is Moderator+, deliberately BROADER than
`GET /admin/team`'s own Owner-only roster: `GET /admin/team` exposes who
currently holds what admin TIER (a management-sensitive fact about
access/authority), while a shift schedule is operational coordination
data whose entire real purpose -- per this feature's own framing,
"a real, shared way to see who is expected to be covering... for
coverage purposes" -- requires every volunteer to see it, not just the
Owner. This matches the READ side of `GET /admin/blacklist/triggers`
(Moderator+) and `GET /admin/appeals` (Senior-Moderator+): several
existing views already give Moderator+ visibility into team-wide
operational data while keeping the corresponding WRITE/manage actions
at a higher tier.

OVERLAP HANDLING: see `app/models/shift.py`'s own module docstring --
allowed, flagged (not blocked), scoped to the same volunteer only.
"""

import uuid

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireOwner
from app.models.enums import AdminRole
from app.models.shift import ShiftAssignment
from app.models.user import User
from app.schemas.shift import (
    ShiftAssignmentCreate,
    ShiftAssignmentOut,
    ShiftAssignmentUpdate,
    ShiftAssignmentWriteResponse,
    ShiftListOut,
)
from app.services import shift_service

router = APIRouter(tags=["admin-shifts"])


def _effective_rank(user: User) -> int:
    """Same small, local, read-only projection `admin_roles.py`'s own
    `list_admin_team` already uses instead of importing
    `app.api.deps._effective_admin_rank` directly (see that endpoint's
    own docstring for why)."""
    if user.is_superuser:
        return AdminRole.OWNER.rank
    if user.admin_role is not None:
        return AdminRole(user.admin_role).rank
    return 0


async def _load_volunteers(db: DbSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    if not user_ids:
        return {}
    result = await db.execute(select(User).where(User.id.in_(user_ids)))
    return {u.id: u for u in result.scalars().all()}


def _to_out(shift: ShiftAssignment, volunteers_by_id: dict[uuid.UUID, User]) -> ShiftAssignmentOut:
    volunteer = volunteers_by_id.get(shift.volunteer_id)
    return ShiftAssignmentOut(
        id=shift.id,
        volunteer_id=shift.volunteer_id,
        volunteer_name=volunteer.full_name if volunteer else None,
        volunteer_email=volunteer.email if volunteer else None,
        start_at=shift.start_at,
        end_at=shift.end_at,
        notes=shift.notes,
        created_by=shift.created_by,
        created_at=shift.created_at,
        updated_at=shift.updated_at,
    )


async def _require_real_volunteer(db: DbSession, volunteer_id: uuid.UUID) -> User:
    """A shift can only be assigned to a real, currently Moderator-or-
    higher user -- a plain student has no moderation duties to cover.
    404s honestly for both "no such user" and "not a real volunteer"
    rather than leaking which of the two it is."""
    target = await db.get(User, volunteer_id)
    if target is None or _effective_rank(target) < AdminRole.MODERATOR.rank:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such volunteer")
    return target


@router.post("/admin/shifts", response_model=ShiftAssignmentWriteResponse, status_code=status.HTTP_201_CREATED)
async def create_shift(
    payload: ShiftAssignmentCreate, current_user: CurrentUser, db: DbSession, _: RequireOwner
) -> ShiftAssignmentWriteResponse:
    await _require_real_volunteer(db, payload.volunteer_id)

    shift, overlapping_ids = await shift_service.create_shift(
        db,
        volunteer_id=payload.volunteer_id,
        start_at=payload.start_at,
        end_at=payload.end_at,
        notes=payload.notes,
        created_by=current_user.id,
    )
    volunteers_by_id = await _load_volunteers(db, {shift.volunteer_id})
    return ShiftAssignmentWriteResponse(shift=_to_out(shift, volunteers_by_id), overlapping_shift_ids=overlapping_ids)


@router.patch("/admin/shifts/{shift_id}", response_model=ShiftAssignmentWriteResponse)
async def update_shift(
    shift_id: uuid.UUID, payload: ShiftAssignmentUpdate, current_user: CurrentUser, db: DbSession, _: RequireOwner
) -> ShiftAssignmentWriteResponse:
    shift = await db.get(ShiftAssignment, shift_id)
    if shift is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such shift")

    if payload.volunteer_id is not None:
        await _require_real_volunteer(db, payload.volunteer_id)

    payload_fields = payload.model_fields_set
    try:
        shift, overlapping_ids = await shift_service.update_shift(
            db,
            shift=shift,
            volunteer_id=payload.volunteer_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            notes=payload.notes,
            notes_provided="notes" in payload_fields,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    volunteers_by_id = await _load_volunteers(db, {shift.volunteer_id})
    return ShiftAssignmentWriteResponse(shift=_to_out(shift, volunteers_by_id), overlapping_shift_ids=overlapping_ids)


@router.delete("/admin/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shift(shift_id: uuid.UUID, current_user: CurrentUser, db: DbSession, _: RequireOwner) -> Response:
    shift = await db.get(ShiftAssignment, shift_id)
    if shift is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such shift")
    await shift_service.delete_shift(db, shift=shift)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/admin/shifts", response_model=ShiftListOut)
async def list_team_shifts(
    current_user: CurrentUser, db: DbSession, _: RequireModerationAccess, include_past: bool = False
) -> ShiftListOut:
    shifts = await shift_service.list_shifts(db, include_past=include_past)
    volunteers_by_id = await _load_volunteers(db, {s.volunteer_id for s in shifts})
    items = [_to_out(s, volunteers_by_id) for s in shifts]
    return ShiftListOut(items=items, total=len(items))


@router.get("/admin/shifts/export.ics")
async def export_team_shifts_ics(
    current_user: CurrentUser, db: DbSession, _: RequireModerationAccess, mine_only: bool = False
) -> Response:
    """Real RFC 5545 calendar export -- the whole real, current+upcoming
    team schedule by default, or (per `mine_only=true`) just the
    calling volunteer's OWN real shifts, so a volunteer can subscribe
    their own calendar app to just their commitments without importing
    everyone else's. Always `include_past=False` here -- a calendar
    subscription is for real, actionable upcoming coverage, not a
    historical record (the JSON `GET /admin/shifts?include_past=true`
    stays the real place for that)."""
    volunteer_filter = current_user.id if mine_only else None
    shifts = await shift_service.list_shifts(db, include_past=False, volunteer_id=volunteer_filter)
    volunteers_by_id = await _load_volunteers(db, {s.volunteer_id for s in shifts})
    ics_content = shift_service.export_to_ics(shifts, volunteers_by_id)
    return Response(
        content=ics_content,
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=apex_shift_schedule.ics"},
    )
