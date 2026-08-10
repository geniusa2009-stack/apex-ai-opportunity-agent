import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DeadlineAlert(Base):
    """One AI-generated preparation milestone for a tracked scholarship
    (Ecosystem Feature 2: AI Smart Deadline Alerts & Calendar Sync) --
    e.g. "30 days before the deadline, start your essays." Generated in
    a batch by `app.services.deadline_alerts.generate_milestones` for a
    given `ApplicationTracker`, and exportable as an `.ics` calendar file
    via that same module's `export_to_ics`.

    No `updated_at`/`TimestampMixin` -- same literal-field-list posture
    already established for `UserHistory`/`StudentMemory`/
    `ApplicationTracker` in the two preceding steps: the ask names
    exactly `id`, `user_id`, `tracker_id`, `title`, `description`,
    `scheduled_date`, `is_completed`, `created_at` for this model.
    """

    __tablename__ = "deadline_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # CASCADE per the ask's explicit instruction -- deleting a tracker
    # should delete its derived milestone alerts too; there's nothing
    # left for an alert to be a milestone toward once its tracker is gone.
    tracker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("application_trackers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_date: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Smart Notifications Engine addition -- NOT part of this model's
    # original literal field list (disclosed above), added to support
    # that step's own "Approaching Deadlines" notification trigger (see
    # `app/workers/deadline_reminder_tasks.py`'s own docstring for the
    # full reasoning). Without this flag, a daily periodic scan for
    # "alerts due within N days" would re-notify the SAME alert every
    # single day it stays inside that window, not just once when it
    # first enters it -- a real, disclosed schema addition to an
    # already-shipped model, not scope invented for its own sake.
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<DeadlineAlert id={self.id} tracker_id={self.tracker_id} "
            f"scheduled_date={self.scheduled_date} title={self.title!r}>"
        )
