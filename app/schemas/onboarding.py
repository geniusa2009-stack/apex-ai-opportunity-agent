"""New-Volunteer Onboarding Checklist -- Pydantic schemas.

Backs the volunteer's own view/mark endpoints
(`app/api/v1/endpoints/onboarding.py`, `/me/onboarding*`) and the
Owner-tier team-wide visibility endpoint (`/admin/onboarding/team`).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import AdminRole


class OnboardingStepOut(BaseModel):
    """One real, centrally defined step (`ONBOARDING_STEPS`,
    app/models/onboarding.py) merged with one volunteer's own real
    completion state."""

    key: str
    order: int
    label: str
    completed: bool
    completed_at: datetime | None = None


class OnboardingChecklistOut(BaseModel):
    """`GET /me/onboarding` response -- the caller's own real checklist."""

    role_granted_at: datetime | None
    steps: list[OnboardingStepOut]
    all_completed: bool


class OnboardingStepActionResponse(BaseModel):
    success: bool
    step_key: str
    completed: bool


class AdminOnboardingMemberOut(BaseModel):
    """One row of `GET /admin/onboarding/team` -- one real volunteer
    holding `MODERATOR` or `SENIOR_MODERATOR`. `OWNER` (both the
    `is_superuser`-only legacy shape and the real bootstrapped
    `admin_role=OWNER` shape) is deliberately excluded entirely -- see
    that endpoint's own docstring on why an Owner has no real
    `role_granted_at` grant event to onboard from."""

    user_id: uuid.UUID
    email: str | None
    full_name: str | None
    admin_role: AdminRole
    role_granted_at: datetime | None
    steps: list[OnboardingStepOut]
    completed_count: int
    total_steps: int
    all_completed: bool


class AdminOnboardingTeamOut(BaseModel):
    items: list[AdminOnboardingMemberOut]
    total: int
