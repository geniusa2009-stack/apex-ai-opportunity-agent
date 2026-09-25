"""Application Intelligence step, Part D/E: explicit user confirmation
that one deterministic, opportunity-derived requirement (from
`application_requirements.extract_requirements`) has been completed.

AUDITED FIRST (Part 0): `ApplicationTracker.ai_todo_list` already lets a
user toggle completion on Gemini-GENERATED prep tasks -- that mechanism
is reused unmodified for those tasks. This table is a SEPARATE,
deliberately small concept: completion state for the DETERMINISTIC
requirement categories this phase derives from the linked Opportunity's
own verified text (see `application_requirements.py`), which have no
existing storage anywhere. Not a duplicate of `ai_todo_list` -- the two
represent genuinely different kinds of checklist items (AI-suggested
prep steps vs. evidence-derived application requirements) and are kept
structurally separate rather than merged into one ambiguous list, per
Part E's own "distinguish USER_CONFIRMED_COMPLETE from SYSTEM_INFERRED"
rule: a row existing here with `is_completed=True` IS the
USER_CONFIRMED_COMPLETE signal -- nothing in this codebase ever inserts
or flips one of these rows automatically (e.g. because a document was
uploaded, per Part N's own explicit warning against that).
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApplicationRequirementCompletion(Base):
    __tablename__ = "application_requirement_completions"
    __table_args__ = (
        UniqueConstraint("tracker_id", "requirement_key", name="uq_app_requirement_completion_tracker_key"),
        Index("ix_app_requirement_completions_tracker_id", "tracker_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    tracker_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("application_trackers.id", ondelete="CASCADE"), nullable=False
    )

    # Matches `RequirementItem.key` from `application_requirements.py`
    # (e.g. "cv_resume", "personal_statement") -- a stable, deterministic
    # slug, not a free-text label, so re-deriving requirements for the
    # same opportunity always maps back onto the same completion row.
    requirement_key: Mapped[str] = mapped_column(String(60), nullable=False)

    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ApplicationRequirementCompletion tracker_id={self.tracker_id} "
            f"requirement_key={self.requirement_key!r} is_completed={self.is_completed}>"
        )
