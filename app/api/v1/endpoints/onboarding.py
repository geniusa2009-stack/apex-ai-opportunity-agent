"""New-Volunteer Onboarding Checklist -- endpoints.

Three real endpoints:
    GET    /me/onboarding                        (Moderator+, own record only)
    POST   /me/onboarding/{step_key}/complete     (Moderator+, own record only)
    DELETE /me/onboarding/{step_key}/complete     (Moderator+, own record only --
                                                    real undo for an accidental
                                                    mark, same claim/release-style
                                                    symmetry this codebase already
                                                    uses elsewhere)
    GET    /admin/onboarding/team                 (Owner-only, whole team)

DISCLOSED: step completion is NOT written to `ModerationLogEntry`.
That table's real, established semantics (every prior phase) are trust-
and-safety ACTIONS taken against a target (a video, a message, a report,
a piece of guild content). Marking your own onboarding step read is a
purely self-administrative event with no target and no trust-and-safety
outcome -- writing it there would blur that log's real meaning for
every other view that already reads it (`GET /admin/activity-log`,
`GET /admin/high-risk`, the CSV export). `completed_at` on
`OnboardingChecklistProgress` itself is the real, sufficient audit trail
for this feature's own real need.

Routing-precedence check (same discipline as every other router in this
codebase): `/me/onboarding*` and `/admin/onboarding/team` share no path
segment position with any other already-registered route (no other
router owns a literal `me/onboarding` or `admin/onboarding` prefix) --
registration order is provably irrelevant.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireOwner
from app.models.enums import AdminRole
from app.models.onboarding import ONBOARDING_STEP_KEYS, ONBOARDING_STEPS, OnboardingChecklistProgress
from app.models.user import User
from app.schemas.onboarding import (
    AdminOnboardingMemberOut,
    AdminOnboardingTeamOut,
    OnboardingChecklistOut,
    OnboardingStepActionResponse,
    OnboardingStepOut,
)

router = APIRouter(tags=["onboarding"])


def _build_steps(completed_keys: set[str], completed_at_by_key: dict[str, object]) -> list[OnboardingStepOut]:
    return [
        OnboardingStepOut(
            key=step.key,
            order=step.order,
            label=step.label,
            completed=step.key in completed_keys,
            completed_at=completed_at_by_key.get(step.key),
        )
        for step in ONBOARDING_STEPS
    ]


async def _load_progress(db: DbSession, user_id: uuid.UUID) -> tuple[set[str], dict[str, object]]:
    result = await db.execute(
        select(OnboardingChecklistProgress).where(OnboardingChecklistProgress.user_id == user_id)
    )
    rows = result.scalars().all()
    completed_keys = {row.step_key for row in rows}
    completed_at_by_key = {row.step_key: row.completed_at for row in rows}
    return completed_keys, completed_at_by_key


@router.get("/me/onboarding", response_model=OnboardingChecklistOut)
async def get_my_onboarding_checklist(
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> OnboardingChecklistOut:
    """Moderator-or-higher. Always the caller's OWN record -- there is no
    `user_id` path/query parameter here at all, so there is no way to
    even ask for someone else's (mirrors `GET /moderation/appeals/mine`'s
    own no-parameter-means-your-own shape)."""
    completed_keys, completed_at_by_key = await _load_progress(db, current_user.id)
    steps = _build_steps(completed_keys, completed_at_by_key)
    return OnboardingChecklistOut(
        role_granted_at=current_user.role_granted_at,
        steps=steps,
        all_completed=len(completed_keys) == len(ONBOARDING_STEPS),
    )


@router.post("/me/onboarding/{step_key}/complete", response_model=OnboardingStepActionResponse)
async def complete_my_onboarding_step(
    step_key: str,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> OnboardingStepActionResponse:
    """Moderator-or-higher, own record only. 404s for a step key that
    isn't one of the real, centrally defined `ONBOARDING_STEPS` -- never
    silently accepts an arbitrary client-invented key (this step's own
    explicit "keep the definitions server-side" instruction). Idempotent:
    marking an already-completed step again is a real, harmless no-op
    (real `INSERT ... ON CONFLICT DO NOTHING`-equivalent via a pre-check,
    not a 409 -- there is no real, useful distinction between "I marked
    this twice" and "I marked this once" from the volunteer's own point
    of view)."""
    if step_key not in ONBOARDING_STEP_KEYS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown onboarding step")

    existing = await db.execute(
        select(OnboardingChecklistProgress).where(
            OnboardingChecklistProgress.user_id == current_user.id,
            OnboardingChecklistProgress.step_key == step_key,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(OnboardingChecklistProgress(user_id=current_user.id, step_key=step_key))
        await db.commit()

    return OnboardingStepActionResponse(success=True, step_key=step_key, completed=True)


@router.delete("/me/onboarding/{step_key}/complete", response_model=OnboardingStepActionResponse)
async def uncomplete_my_onboarding_step(
    step_key: str,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> OnboardingStepActionResponse:
    """Moderator-or-higher, own record only. Real undo for an accidental
    mark -- same reversible-toggle posture the claim system's own
    `POST`/`DELETE .../claim` pair already established in this codebase.
    Idempotent the same way `complete_my_onboarding_step` is: un-marking
    an already-incomplete step is a harmless no-op, not a 404/409."""
    if step_key not in ONBOARDING_STEP_KEYS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown onboarding step")

    await db.execute(
        delete(OnboardingChecklistProgress).where(
            OnboardingChecklistProgress.user_id == current_user.id,
            OnboardingChecklistProgress.step_key == step_key,
        )
    )
    await db.commit()

    return OnboardingStepActionResponse(success=True, step_key=step_key, completed=False)


@router.get("/admin/onboarding/team", response_model=AdminOnboardingTeamOut)
async def get_team_onboarding_progress(
    current_user: CurrentUser,
    db: DbSession,
    _: RequireOwner,
) -> AdminOnboardingTeamOut:
    """Owner-only. Every real volunteer holding `MODERATOR` or
    `SENIOR_MODERATOR` -- DELIBERATELY excludes `OWNER` entirely (both
    the `is_superuser=True`-only legacy shape AND the real bootstrapped
    `admin_role=OWNER` shape `scripts/bootstrap_first_owner.py` sets --
    unlike `GET /admin/team`'s own broader `or_(is_superuser,
    admin_role.isnot(None))` roster query). This view answers "how is my
    NEW-VOLUNTEER onboarding going," matching this step's own explicit
    framing ("someone just granted Moderator or Senior Moderator tier");
    `OWNER` can never even be granted through the real, tracked
    `PATCH /admin/users/{id}/role` flow this checklist hooks into (that
    endpoint's own validator rejects it -- see `AdminRoleAssignRequest`'s
    docstring), so an Owner row here would never have a real
    `role_granted_at` grant event to report progress against -- showing
    one as a perpetually-0%-onboarded row would be a fabricated,
    misleading signal, not an honest one.
    """
    result = await db.execute(
        select(User)
        .where(User.admin_role.in_([AdminRole.MODERATOR, AdminRole.SENIOR_MODERATOR]))
        .order_by(User.role_granted_at.asc().nulls_last())
    )
    users = result.scalars().all()

    items: list[AdminOnboardingMemberOut] = []
    for user in users:
        completed_keys, completed_at_by_key = await _load_progress(db, user.id)
        steps = _build_steps(completed_keys, completed_at_by_key)
        items.append(
            AdminOnboardingMemberOut(
                user_id=user.id,
                email=user.email,
                full_name=user.full_name,
                admin_role=user.admin_role,
                role_granted_at=user.role_granted_at,
                steps=steps,
                completed_count=len(completed_keys),
                total_steps=len(ONBOARDING_STEPS),
                all_completed=len(completed_keys) == len(ONBOARDING_STEPS),
            )
        )

    return AdminOnboardingTeamOut(items=items, total=len(items))
