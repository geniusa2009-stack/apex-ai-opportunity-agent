"""APEX User Intelligence + Proactive Guidance step, Part D: a minimal,
extensible, append-only behavioral-event log linking a user to a real
catalog `Opportunity`.

AUDITED FIRST (Part 0): `UserBookmark` already tracks SAVED/APPLIED with
its own real `created_at`; this table deliberately does NOT duplicate
that (see `InteractionEventType`'s own docstring, app/models/enums.py).
This model exists only for the events genuinely missing from the
codebase before this phase: VIEWED, DISMISSED, MARKED_NOT_RELEVANT,
SHARED, MORE_LIKE_THIS (plus two listed-but-unwired types, see the enum
docstring).
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OpportunityInteraction(Base):
    """One row per explicit user->opportunity behavioral event.

    No `updated_at` -- an interaction event is an immutable fact about a
    moment in time (same posture as `Notification`/`UserHistory`/
    `ApplicationTracker`'s own `created_at`-only design in this
    codebase), never edited after creation.

    `event_metadata` is nullable JSONB for event-specific structured
    context (e.g. `{"feedback_reason": "..."}` on a MARKED_NOT_RELEVANT
    row) -- NEVER a place for sensitive attributes (Part P); nothing in
    this codebase writes user-sensitive fields into it.
    """

    __tablename__ = "opportunity_interactions"
    __table_args__ = (
        Index("ix_opportunity_interactions_user_id_created_at", "user_id", "created_at"),
        Index("ix_opportunity_interactions_opportunity_id_created_at", "opportunity_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )

    # Validated against `InteractionEventType` at the service layer
    # (app/services/interaction_service.py) -- see that enum's own
    # docstring (app/models/enums.py) for why this stays a plain String.
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)

    event_metadata: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityInteraction user_id={self.user_id} "
            f"opportunity_id={self.opportunity_id} event_type={self.event_type!r}>"
        )
