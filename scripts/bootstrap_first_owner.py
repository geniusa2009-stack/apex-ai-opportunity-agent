"""One-off bootstrap script -- Real Tiered Role System step.

Grants a user BOTH `is_superuser = True` AND `admin_role = OWNER` --
this is the ONE deliberate, disclosed exception to "everything after the
first Owner goes through the real API" (`PATCH /admin/users/{user_id}/
role`, `app/api/v1/endpoints/admin_roles.py`): that endpoint is ITSELF
Owner-gated, so nothing could ever call it to create the very first
Owner a fresh deployment has. Real bootstrapping problem, not a design
oversight -- see `AdminRole.OWNER`'s own docstring (app/models/enums.py)
for the fuller reasoning, and `AdminRoleAssignRequest`'s own validator
for where this is enforced (that endpoint hard-rejects `new_role=OWNER`
with a real 422, so this can't accidentally happen through the API
either).

NOT a permanent endpoint or Celery task -- run manually, once per
deployment, directly against the real database, per this step's own
explicit "one-time manual step... don't build unnecessary API surface
for a thing that happens once per deployment" instruction.

WHY BOTH `is_superuser` AND `admin_role=OWNER` are set together (not
just `admin_role=OWNER` alone): `app/api/deps.py::_effective_admin_rank`
already treats `is_superuser=True` as a legacy alias for `OWNER` rank,
so setting `admin_role=OWNER` alone would already be sufficient for
every NEW tiered check this step adds (`RequireOwner`,
`RequireSeniorModerationAccess`, `RequireModerationAccess`). But
`is_superuser` is ALSO the gate on several pre-existing, OUT-OF-SCOPE-
for-this-step admin surfaces this task did not touch (course-authoring
in `courses.py`, video upload in `media_router.py`, and the original
`RequireAdmin` dependency itself, still used by both) -- the very first
real Owner almost certainly needs those too (they're the same person
who'd otherwise be the sole `is_superuser` account today). Setting only
`admin_role` would leave the first Owner locked out of those unrelated
surfaces, a real regression this script avoids by setting both.

Usage:
    # By email (must already exist -- sign in once via the real app/
    # Supabase flow first so the row exists via auto-provisioning; see
    # app/api/deps.py::_resolve_or_provision_user):
    python -m scripts.bootstrap_first_owner --email you@example.com

    # By user id directly:
    python -m scripts.bootstrap_first_owner --user-id <uuid>

    # Preview only, writes nothing:
    python -m scripts.bootstrap_first_owner --email you@example.com --dry-run
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db.session import AsyncSessionLocal, engine  # noqa: E402
from app.models.enums import AdminRole  # noqa: E402
from app.models.user import User  # noqa: E402


async def _run(*, email: str | None, user_id: uuid.UUID | None, dry_run: bool) -> dict:
    async with AsyncSessionLocal() as db:
        if user_id is not None:
            user = await db.get(User, user_id)
        else:
            result = await db.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()

        if user is None:
            return {
                "status": "NOT FOUND",
                "detail": (
                    f"No user matching {'id=' + str(user_id) if user_id else 'email=' + str(email)}. "
                    "The target person must sign in at least once via the real app/Supabase flow "
                    "first (see app/api/deps.py's auto-provisioning path) so this row exists."
                ),
            }

        before = {"is_superuser": user.is_superuser, "admin_role": user.admin_role}

        if dry_run:
            return {
                "status": "DRY RUN (nothing written)",
                "user_id": str(user.id),
                "email": user.email,
                "before": {
                    "is_superuser": before["is_superuser"],
                    "admin_role": before["admin_role"].value if before["admin_role"] else None,
                },
                "would_set": {"is_superuser": True, "admin_role": AdminRole.OWNER.value},
            }

        user.is_superuser = True
        user.admin_role = AdminRole.OWNER
        db.add(user)
        await db.commit()
        await db.refresh(user)

        return {
            "status": "OWNER GRANTED (real write committed)",
            "user_id": str(user.id),
            "email": user.email,
            "before": {
                "is_superuser": before["is_superuser"],
                "admin_role": before["admin_role"].value if before["admin_role"] else None,
            },
            "after": {"is_superuser": user.is_superuser, "admin_role": user.admin_role.value},
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email", type=str, help="Email of the existing user to grant Owner to.")
    target.add_argument("--user-id", type=str, help="UUID of the existing user to grant Owner to.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview the change without writing anything."
    )
    args = parser.parse_args()

    user_id = uuid.UUID(args.user_id) if args.user_id else None

    try:
        result = asyncio.run(_run(email=args.email, user_id=user_id, dry_run=args.dry_run))
    finally:
        asyncio.run(engine.dispose())

    print("\n=== Bootstrap First Owner -- result ===")
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
