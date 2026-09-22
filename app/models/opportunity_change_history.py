"""Taxonomy Evolution + Temporal State Machine + Change History step,
Part C: a dedicated, append-only historical record for meaningful
`Opportunity` field changes.

DISCLOSED, CONFIRMED NECESSARY ADDITION (Part 0 audit): no existing
mechanism in this codebase preserves before/after values for
`Opportunity` field changes -- `upsert_opportunity`'s real UPDATE branch
(`app/services/aggregator_service.py`) already overwrites `title`/
`description`/`type`/`deadline`/`country`/`is_local_egypt`/
`target_age_group` in place with no history kept (confirmed by direct
read before writing this model). `Evidence` (D6) is append-only too, but
it records SOURCE CLAIMS about a fact, not the canonical row's own
before/after value -- a distinct, complementary concept, not a
duplicate of this table (Part 20 discipline: this is genuinely new
capability, not a second Evidence model).

IMMUTABILITY (same convention as `Evidence`'s own docstring): enforced
by convention, not a DB trigger -- `change_history_service.
record_change_if_meaningful` is the only real write path, and it only
ever INSERTs. Nothing in this codebase ever UPDATEs an existing
`OpportunityChangeHistory` row.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OpportunityChangeHistory(Base):
    """One immutable record of one meaningful field change on one
    `Opportunity`. `old_value`/`new_value` are stored as plain text
    (JSON-encoded by the caller for structured values, e.g. a
    `target_academic_levels` list) -- this table makes no assumption
    about a field's real type, matching `Claim.claim_value`'s own
    plain-text posture.

    `source_id`/`evidence_id` are both nullable: a change record MUST be
    created only when the caller genuinely knows why a value changed
    (Part C2's own "do not create a change record without knowing why"
    rule), but "why" is not always resolvable to one specific `Source`/
    `Evidence` row (e.g. a state-machine-driven `lifecycle_state`
    transition derived from an aggregate D6 decision spanning several
    evidence rows) -- `change_reason` always carries a real, non-empty
    explanation regardless, so nothing here is ever a still-unexplained
    change even when a single evidence pointer isn't meaningful.
    """

    __tablename__ = "opportunity_change_history"
    __table_args__ = (
        Index("ix_opportunity_change_history_opportunity_id", "opportunity_id"),
        Index("ix_opportunity_change_history_changed_at", "changed_at"),
        Index("ix_opportunity_change_history_source_id", "source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )

    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence.id", ondelete="SET NULL"), nullable=True
    )

    change_reason: Mapped[str] = mapped_column(Text, nullable=False)

    changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OpportunityChangeHistory id={self.id} opportunity_id={self.opportunity_id} "
            f"field={self.field_name!r}>"
        )
