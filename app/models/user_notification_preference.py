"""Opportunity Monitoring & Personal Impact Engine step, Part O: the
minimum notification-preference structure this phase actually needs.

AUDITED FIRST (Part 0): no notification-preference table or column
exists anywhere in this codebase -- `notification_service.py`'s own
`HIGHLY_CRITICAL_TYPES` docstring explicitly flags per-user preferences
as "a real, reasonable follow-up... flagged rather than built
speculatively." This phase is that follow-up, scoped to exactly the
three categories Part O itself names -- NOT a generic preference
framework with channel-level (push/email/in-app) granularity, digest
scheduling, or per-opportunity-type filters, none of which any part of
this task asks for.

One row per user, created lazily on first read/write (see
`notification_preferences.py`) rather than backfilled for every
existing user -- a user with no row is simply treated as "everything on"
(every column's own real default), which is both the least-surprising
behavior for a feature nobody has touched yet and avoids a migration
data-backfill step for a table that's a pure preference cache.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserNotificationPreference(Base):
    """`user_id` is both the primary key and the FK -- a genuine
    one-to-one (not one-to-many) relationship, so there is no separate
    surrogate `id` column to keep in sync with anything (same posture as
    any other pure 1:1 settings-row table would take in this codebase).

    Three boolean opt-outs, matching Part O's own literal three example
    preferences:
    - `opportunity_changes`: eligibility/funding/lifecycle change
      notifications for opportunities this user has saved or applied to.
    - `deadline_alerts`: deadline-change notifications specifically (a
      user may want to keep these on while opting out of the broader
      `opportunity_changes` category, or vice versa -- deadlines are the
      single most action-critical category, so this phase keeps it as
      its own switch rather than folding it into `opportunity_changes`).
    - `application_alerts`: notifications scoped to a user's own active
      `ApplicationTracker` rows (Part I's application-impact guidance's
      notification-layer counterpart).

    All three default to `True` at both the Python and DB level -- opt-
    OUT, not opt-in, matching every other notification this codebase
    already sends without asking first (`deadline_reminder_tasks.py`'s
    own WARNING notifications, Part O's own "respect user opt-outs"
    phrasing implies an existing on-by-default state to opt out FROM).
    """

    __tablename__ = "user_notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )

    opportunity_changes: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    deadline_alerts: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    application_alerts: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserNotificationPreference user_id={self.user_id}>"
