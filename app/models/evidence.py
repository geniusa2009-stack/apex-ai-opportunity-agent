"""D6 (Evidence & Verification Intelligence): a durable, append-only
evidence record -- `Evidence {opportunity_id, claim_id, source_id,
source_url, source_type, source_authority, evidence_text,
evidence_hash, retrieved_at, published_at, valid_from, valid_until,
supports_claim, extraction_method, retrieval_method, metadata}`.

IMMUTABILITY (spec section 5): enforced by CONVENTION, not a DB
trigger -- same posture this codebase already established for
`ResearchTrajectoryStep` (D3): `evidence_verification.record_evidence`
is the only real write path, and it only ever INSERTs a new row (via
`evidence_hash` dedup, see that function's own docstring); nothing in
this codebase's services ever UPDATEs `evidence_text`/`source_url` on
an existing `Evidence` row. If a source's content changes, a NEW
`Evidence` row is created so historical provenance is preserved.

DISCLOSED NAMING DEVIATION (same reason as `ResearchTrajectory
.trajectory_metadata`): the `metadata` field name from the spec collides
with SQLAlchemy's own reserved `Base.metadata` attribute -- this column
is `evidence_metadata` instead, same JSONB shape/purpose.
"""

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SourceAuthority

if TYPE_CHECKING:
    pass


class Evidence(Base):
    """One immutable, real piece of evidence for one `Claim`. `source_id`
    is nullable (a piece of evidence may cite a raw `source_url` that
    hasn't been registered as a `Source` row yet), but
    `evidence_verification.record_evidence` rejects an evidence record
    that has neither a real `source_id` nor a well-formed `source_url`
    -- "no source -> no factual claim" (D6 constitution, rule 1)."""

    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_opportunity_id", "opportunity_id"),
        Index("ix_evidence_claim_id", "claim_id"),
        Index("ix_evidence_source_id", "source_id"),
        Index("ix_evidence_evidence_hash", "evidence_hash"),
        Index("ix_evidence_retrieved_at", "retrieved_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )

    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # `SourceAuthority` value -- see that enum's own docstring for the
    # disclosed, deterministic-rules-only classification limitation.
    source_authority: Mapped[str] = mapped_column(String(20), nullable=False, default=SourceAuthority.UNKNOWN.value)

    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256(normalized_source_url + normalized_evidence_text + claim_type)
    # -- see `evidence_verification.compute_evidence_hash`. Used for
    # dedup (identical evidence for the same claim is not re-inserted)
    # and, in a future D8 pass, unchanged-vs-changed detection.
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    retrieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # True = supports the claim, False = contradicts it, None = neutral/
    # unclear (e.g. background text that mentions the claim without
    # confirming or denying it).
    supports_claim: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # e.g. "ai_gateway_structured_output" (D6's own extension of the
    # existing `ai_gateway.verify_and_embed` call) vs "manual" (a human
    # moderator attaching evidence directly).
    extraction_method: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    retrieval_method: Mapped[str] = mapped_column(String(50), nullable=False, default="automated")

    evidence_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    claim: Mapped["Claim"] = relationship(back_populates="evidence", lazy="joined")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Evidence id={self.id} claim_id={self.claim_id} authority={self.source_authority!r}>"
