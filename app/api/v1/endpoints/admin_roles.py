"""Real Tiered Role System step -- Owner-only role management.

Two endpoints, both `RequireOwner`-gated (see `app/api/deps.py` and
`AdminRole`'s own docstring, app/models/enums.py, for the full tier
design this builds on):

    PATCH /admin/users/{user_id}/role  -- assign/revoke Senior Moderator
                                           or Moderator for another user
    GET   /admin/team                  -- list everyone with a real
                                           elevated tier

DISCLOSED, NO NEW SURFACE FOR `OWNER` GRANTS: `PATCH .../role` rejects
`new_role=OWNER` at the Pydantic layer (`AdminRoleAssignRequest`'s own
validator) -- the very first `OWNER` for a deployment is a one-time
bootstrap problem (this endpoint is ITSELF Owner-gated, so nothing could
ever call it to create the first Owner) and is handled once via
`scripts/bootstrap_first_owner.py`, a direct-DB/script step, not new API
surface built for something that happens once per deployment. See that
script's own module docstring for the real bootstrap instructions.

Routing-precedence check (same discipline as every other router in this
codebase): this router's two literal paths (`admin/users/{user_id}/role`,
`admin/team`) diverge at the SECOND path segment (`users`/`team`) from
every other `admin/*` route already registered (`admin/videos/*` in
`admin_moderation.py`, `admin/courses/*` in `media_router.py`) -- no
FastAPI/Starlette path-template ambiguity, registration order is
provably irrelevant.
"""

import datetime as dt
import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, or_, select

from app.api.deps import CurrentUser, DbSession, RequireOwner
from app.models.enums import AdminRole
from app.models.onboarding import OnboardingChecklistProgress
from app.models.user import User
from app.schemas.admin_roles import (
    AdminRoleAssignRequest,
    AdminRoleAssignResponse,
    TeamListResponse,
    TeamMemberOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-roles"])


@router.patch("/admin/users/{user_id}/role", response_model=AdminRoleAssignResponse)
async def assign_admin_role(
    user_id: uuid.UUID,
    payload: AdminRoleAssignRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireOwner,
) -> AdminRoleAssignResponse:
    """Owner-only. Sets `target_user.admin_role` to `payload.new_role`
    (already validated to be `SENIOR_MODERATOR`, `MODERATOR`, or `null`
    -- never `OWNER` -- at the Pydantic layer; see `AdminRoleAssignRequest`'s
    own docstring).

    404s for a target user that doesn't exist -- same posture as every
    other `{id}`-parameterized admin route in this codebase
    (`admin_moderation.py`'s `{video_id}` routes).

    DISCLOSED GUARD: an Owner may not change their OWN role through this
    endpoint (400, not a silent no-op) -- self-service role changes here
    could let an Owner accidentally strip their own access with no
    recovery path other than the same direct-DB bootstrap step this
    endpoint exists specifically to avoid needing more than once. This
    does NOT block an Owner from managing another Owner's `admin_role`
    column (there is no such column value to manage for an Owner
    anyway -- Owners reach `OWNER` rank via `is_superuser`, which this
    endpoint never touches; see this module's own docstring).

    Does NOT touch `is_superuser` at all, in either direction -- that
    column stays exactly what the original bootstrap step (or a real
    `is_superuser=True` legacy account) set it to. Confirmed real: this
    endpoint literally never assigns to that attribute anywhere in this
    function's body.
    """
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot change your own role through this endpoint.",
        )

    target_user = await db.get(User, user_id)
    if target_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # New-Volunteer Onboarding Checklist addition -- a real, FIRST-TIME
    # grant (no elevated `admin_role` before this call) is the one real
    # "this is a new volunteer" event: stamp `role_granted_at` and reset
    # any stale onboarding progress so their checklist starts fresh. A
    # LATERAL change between two elevated tiers (MODERATOR <->
    # SENIOR_MODERATOR) deliberately does NOT reset either -- that
    # person is not a new volunteer, just re-tiered, and re-forcing them
    # through onboarding again would be real, unwarranted busywork this
    # step was never asked to create. A revocation (`new_role=None`)
    # clears `role_granted_at` back to `None` -- honestly reflecting
    # "this person does not currently hold a real grant" -- but leaves
    # any existing progress rows alone (harmless: the next real grant,
    # if any, resets them anyway; there is no real reason to race a
    # delete against a revocation that a re-grant will supersede).
    was_ungranted = target_user.admin_role is None
    if was_ungranted and payload.new_role is not None:
        target_user.role_granted_at = dt.datetime.now(dt.timezone.utc)
        await db.execute(
            delete(OnboardingChecklistProgress).where(OnboardingChecklistProgress.user_id == target_user.id)
        )
    elif payload.new_role is None:
        target_user.role_granted_at = None

    target_user.admin_role = payload.new_role
    db.add(target_user)
    await db.commit()
    await db.refresh(target_user)

    logger.info(
        "Owner %s set admin_role=%s for user %s",
        current_user.id, payload.new_role.value if payload.new_role else None, user_id,
    )

    return AdminRoleAssignResponse(user_id=target_user.id, admin_role=target_user.admin_role)


@router.get("/admin/team", response_model=TeamListResponse)
async def list_admin_team(current_user: CurrentUser, db: DbSession, _: RequireOwner) -> TeamListResponse:
    """Owner-only. Every user with a real elevated tier -- `is_superuser
    = True` (the legacy `OWNER`-alias path) OR a non-null `admin_role` --
    what the eventual `admin-moderation.html` team-view widget reads
    from (see this step's own ask, which names this endpoint as exactly
    that data source).

    No pagination -- see `TeamListResponse`'s own docstring for why this
    list is bounded by construction, unlike `GET /admin/videos/
    review-queue`'s genuinely unbounded queue.

    `effective_role` is always the RESOLVED tier per user, computed with
    the exact same rule as `app/api/deps.py::_effective_admin_rank`
    (kept as a small, local, read-only projection here rather than
    importing that function directly, since this needs the `AdminRole`
    VALUE back, not just an integer rank for a >= comparison) -- a
    caller of this endpoint should never need to know about the
    `is_superuser` legacy-alias mechanics to understand who has what
    access.
    """
    result = await db.execute(
        select(User)
        .where(or_(User.is_superuser.is_(True), User.admin_role.isnot(None)))
        .order_by(User.created_at.asc())
    )
    users = result.scalars().all()

    items = [
        TeamMemberOut(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            effective_role=AdminRole.OWNER if user.is_superuser else user.admin_role,
            is_superuser=user.is_superuser,
            admin_role=user.admin_role,
            created_at=user.created_at,
        )
        for user in users
    ]

    return TeamListResponse(items=items, total=len(items))
