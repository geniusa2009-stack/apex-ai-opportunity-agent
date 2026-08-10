"""Student Dashboard & Admin Control Panel -- response schemas for the two
aggregated hub endpoints (`app/api/v1/endpoints/dashboards.py`).

Every nested section below reuses an ALREADY-EXISTING schema wherever one
already covers the shape this step's ask names (`NotificationOut`,
`StudentVideoMyUploadOut`, `OpportunityOut`, `BadgeOut`) -- this file only
defines the genuinely NEW shapes: the two envelopes themselves, plus the
handful of small, dashboard-specific sections (a trimmed gamification
summary with the new XP-progress field, a lighter moderation-queue row
than the full `AdminVideoReviewOut`, the banned-users list, and platform
stats) that no existing schema already models.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import StudentVideoStatus
from app.schemas.gamification import BadgeOut
from app.schemas.notification import NotificationOut
from app.schemas.opportunity import OpportunityOut
from app.schemas.student_videos import StudentVideoMyUploadOut

# =============================================================================
# Student Hub -- GET /api/v1/dashboards/student
# =============================================================================


class DashboardGamificationOut(BaseModel):
    """This step's own literal "Gamification Stats: current_streak,
    total_xp, badges" -- plus `next_badge_xp`, a disclosed addition
    backing the ask's own "progress bar for XP" frontend requirement.
    See `gamification_service.get_next_badge_threshold`'s own docstring
    for why a badge threshold (not an invented "level" concept) is what
    that progress bar tracks toward, and why this is `None` once every
    defined badge has been unlocked.
    """

    current_streak: int
    total_xp: int
    badges: list[BadgeOut]
    next_badge_xp: int | None = Field(
        default=None,
        description="required_xp of the next locked badge, or null if every "
        "defined badge has already been unlocked (a maxed-out progress bar).",
    )


class StudentDashboardOut(BaseModel):
    """`GET /api/v1/dashboards/student` response -- this step's own four
    literal sections, one field each."""

    gamification: DashboardGamificationOut
    recent_notifications: list[NotificationOut]
    videos: list[StudentVideoMyUploadOut]
    opportunities: list[OpportunityOut]


# =============================================================================
# Admin Hub -- GET /api/v1/dashboards/admin
# =============================================================================


class DashboardModerationItemOut(BaseModel):
    """One row of the admin hub's "UGC Moderation Queue" widget.

    Deliberately a LIGHTER shape than `admin_moderation.py`'s own
    `AdminVideoReviewOut` (which additionally splits `ai_feedback` vs.
    `pipeline_error`, carries `admin_notes`, etc.) -- this is a dashboard
    HUB widget, meant to surface "how many/which videos need attention"
    at a glance with quick Approve/Reject actions, not a full audit view.
    The dedicated `/admin/moderation` page (already built, Admin Override
    Dashboard & AI Decision Correction System step) remains the place for
    the deeper per-video review; this widget links out to it rather than
    re-implementing it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    student_id: UUID
    student_name: str | None
    status: StudentVideoStatus
    created_at: datetime
    thumbnail_url: str | None


class BannedUserOut(BaseModel):
    """One row of the admin hub's "Chat Moderation" widget -- this step's
    own literal "List of currently banned users from Redis
    (`is_user_banned`)" requirement. `display_name` falls back to
    `"Unknown user"` only for the rare case where the Redis key's own
    user id no longer resolves to a real `User` row (see the router's
    own docstring on why this can legitimately happen) -- never a raw
    `None` rendered directly in the admin UI.
    """

    user_id: UUID
    display_name: str
    remaining_seconds: int


class PlatformStatsOut(BaseModel):
    """This step's own literal "Platform Metrics: Count of total registered
    users, total active video hours, and total scraped opportunities."

    DISCLOSED REVISION from an earlier pass of this same milestone: that
    pass modeled the second metric as `total_active_videos` (a plain
    COUNT of APPROVED rows). This re-ask is more specific -- literally
    "video hours," a duration SUM, not a row count -- so
    `total_active_video_hours` replaces it here (see the router's own
    computation for how `StudentVideo.duration_seconds` feeds it).
    """

    total_users: int
    total_active_video_hours: float = Field(
        description="Sum of duration_seconds across all APPROVED videos, "
        "converted to hours and rounded to 1 decimal place."
    )
    total_scraped_opportunities: int


class AdminDashboardOut(BaseModel):
    """`GET /api/v1/dashboards/admin` response -- this step's own three
    literal sections, one field each."""

    moderation_queue: list[DashboardModerationItemOut]
    banned_users: list[BannedUserOut]
    platform_stats: PlatformStatsOut
