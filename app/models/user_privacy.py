"""APEX Privacy + Social Identity Layer -- the server-side privacy model
for profile-level fields.

AUDITED FIRST: no privacy/visibility column or table existed anywhere in
this codebase before this step. `GET /users/search` returned the full
`UserOut` shape (email, phone, gpa, gender, and every profile field) to
any signed-in user with zero restriction -- the single confirmed,
highest-severity finding of this audit. Sensitive fields (email, phone,
gpa, gender, internal account data) are NOT made configurable here --
they are simply never included in any cross-user response; there is
nothing for a user to "set" about them, so no column exists for them.

Scope, deliberately the smallest robust model rather than a per-field
row for all ~15 profile columns: one row per user, one visibility enum
per DATA CATEGORY (not per individual column), covering exactly the
categories `GET /users/search` and the new `GET /users/{user_id}/profile`
endpoint actually need to filter:
  - `bio_visibility`        -- the free-text bio.
  - `education_visibility`  -- degree_level/major/academic_level/
                                 graduation_year (these always travel
                                 together on every profile card in this
                                 codebase's own frontend -- mentors.html,
                                 mentor-profile.html -- so one switch for
                                 all four, not four).
  - `skills_visibility`     -- the skills list.
  - `location_visibility`   -- target_country/nationality/residency_country.
  - `languages_visibility`  -- the languages list.

DEFAULT = "public" for every category, not "connections" or "private".
This is a deliberate, disclosed choice, not an oversight: every one of
these fields is ALREADY fully exposed today (via the unrestricted
`UserOut` search response), and `mentors.html`/`mentor-profile.html`
have no other data source for browsing mentors (no `GET /users/{id}`
existed before this step). A "safe conservative default" here means
"don't silently make something MORE exposed than it already is," not
"silently break the mentor directory by defaulting to less exposed than
today." Defaulting to "public" preserves today's real behavior exactly;
the new, real capability this step adds is that a user can now actively
DIAL THIS DOWN to "connections" or "private" through a real, persisted,
server-enforced setting -- which did not exist before at all.

`PERSONAL INTELLIGENCE` fields (interests, application_goals,
preferred_opportunity_types, funding_preference, remote_preference,
deadline_lead_time_days, target_country's *use* for matching) are
deliberately NOT covered by any column here: they are never returned in
any cross-user response regardless of settings (self-only, always), per
the "social visibility != personalization" distinction -- hiding a field
from other students must never mean APEX itself stops using it to
personalize that student's own recommendations.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Plain String column, `Literal`-validated at the schema layer on write
# -- same established pattern as `AchievementPost.privacy_level` and
# `UserConnection.status` in this codebase (see app/schemas/privacy.py's
# `PrivacyVisibility` for the enforced literal).
_DEFAULT_VISIBILITY = "public"


class UserPrivacySettings(Base):
    """`user_id` is both the primary key and the FK -- a genuine 1:1
    settings row, same posture as `UserNotificationPreference`. Created
    lazily on first read/write (see `app/services/privacy_service.py`),
    never backfilled -- a user with no row is treated as "every category
    at its real default" (see module docstring), which is both the
    least-surprising behavior for a user who has never opened the
    Privacy & Visibility panel and avoids a migration data-backfill step
    for a table that's a pure settings cache.
    """

    __tablename__ = "user_privacy_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    bio_visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=_DEFAULT_VISIBILITY, server_default=_DEFAULT_VISIBILITY
    )
    education_visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=_DEFAULT_VISIBILITY, server_default=_DEFAULT_VISIBILITY
    )
    skills_visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=_DEFAULT_VISIBILITY, server_default=_DEFAULT_VISIBILITY
    )
    location_visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=_DEFAULT_VISIBILITY, server_default=_DEFAULT_VISIBILITY
    )
    languages_visibility: Mapped[str] = mapped_column(
        String(20), nullable=False, default=_DEFAULT_VISIBILITY, server_default=_DEFAULT_VISIBILITY
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserPrivacySettings user_id={self.user_id}>"
