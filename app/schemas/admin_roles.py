"""Real Tiered Role System step -- schemas for the two new Owner-only
endpoints (`PATCH /admin/users/{user_id}/role`, `GET /admin/team`). See
`AdminRole`'s own docstring (app/models/enums.py) for the full tier
design this step builds on.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

from app.models.enums import AdminRole


class AdminRoleAssignRequest(BaseModel):
    """`PATCH /admin/users/{user_id}/role` request body.

    `new_role` accepts `SENIOR_MODERATOR`, `MODERATOR`, or `null`
    (revoke any elevated tier) -- and DELIBERATELY REJECTS `OWNER`,
    rather than merely documenting that this endpoint "shouldn't" be
    used to grant it. See `AdminRole.OWNER`'s own docstring for why: the
    first `OWNER` for a deployment is a genuine bootstrapping problem
    (this endpoint is itself Owner-gated, so nobody could ever call it
    to create the first one), handled once via `scripts/
    bootstrap_first_owner.py`, not through this API at all. Enforcing
    that here (see `_reject_owner_grant` below) means a bug can't
    silently let one Owner mint another Owner through this surface --
    it's a real 422, not just a documented expectation.
    """

    new_role: AdminRole | None

    @field_validator("new_role")
    @classmethod
    def _reject_owner_grant(cls, value: AdminRole | None) -> AdminRole | None:
        if value is AdminRole.OWNER:
            raise ValueError(
                "OWNER cannot be granted through this endpoint -- it is a one-time "
                "bootstrap step (see scripts/bootstrap_first_owner.py)."
            )
        return value


class AdminRoleAssignResponse(BaseModel):
    """`PATCH /admin/users/{user_id}/role` response."""

    user_id: uuid.UUID
    admin_role: AdminRole | None


class TeamMemberOut(BaseModel):
    """One row of `GET /admin/team`'s response -- any user with a real
    elevated tier, i.e. `is_superuser=True` (the legacy `OWNER`-alias
    path -- see `app/api/deps.py::_effective_admin_rank`) OR a non-null
    `admin_role`. `effective_role` is always the RESOLVED tier (never
    raw column values), so this list reads correctly regardless of
    which of the two signals actually granted it -- a caller of this
    endpoint should never need to know about the `is_superuser` legacy
    alias to understand who has what access.
    """

    user_id: uuid.UUID
    email: str | None
    full_name: str | None
    effective_role: AdminRole
    is_superuser: bool
    admin_role: AdminRole | None
    created_at: datetime


class TeamListResponse(BaseModel):
    """`GET /admin/team` response -- a plain list, not `PaginatedResponse`.
    DISCLOSED, DELIBERATE CHOICE: this list is bounded by construction
    (every real volunteer/admin a deployment has, not an unbounded
    user-facing collection) -- pagination machinery would be over-
    engineering for a list this small by definition; every other truly
    unbounded admin list in this codebase (`GET /admin/videos/review-queue`)
    already uses `PaginatedResponse`, and this one deliberately does not,
    for that reason.
    """

    items: list[TeamMemberOut]
    total: int
