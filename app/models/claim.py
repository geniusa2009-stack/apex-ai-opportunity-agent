"""D6 (Evidence & Verification Intelligence): normalized claim
representation -- `Opportunity -> Claim {claim_type, claim_value,
normalized_value, status, importance, evidence links}`.

STEP 1 INVESTIGATION, real claim vocabulary: `app/services/ai.py`'s
`VerifiedOpportunitySchema` is the one real structured-output shape this
codebase's AI verification call actually produces today (title,
deadline, eligibility_criteria, funding_details, is_legitimate,
confidence_score, min_age, max_age, target_gender,
target_academic_levels). D6's `ALLOWED_CLAIM_TYPES`
(`app/services/evidence_verification.py`) is built around those real
fields plus `country` (D5.1's `country_hint`) and `application_url`
(`Opportunity.target_url`) -- not the full ~15-entry example list D6's
own spec shows, per that spec's own instruction not to invent claim
types the existing schema has no equivalent extraction for.

`status` holds an `EvidenceStatus` value (SUPPORTED/CONTRADICTED/
UNSUPPORTED/STALE/CONFLICTED/UNKNOWN) -- always backend-derived by
`evidence_verification.evaluate_claim`, never LLM-chosen. `importance`
is a small open string ("critical"/"normal") rather than a closed enum,
matching this codebase's own established "not every workflow field
needs a Postgres ENUM" posture (see `Source.source_type`'s own
docstring).
"""

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import EvidenceStatus

if TYPE_CHECKING:
    from app.models.evidence import Evidence


class Claim(Base):
    """One normalized, evaluable claim about an `Opportunity` (e.g.
    claim_type="deadline", claim_value="2026-12-01"). Backend-owned --
    an LLM may propose a candidate claim value, but never sets `status`
    directly (see `evidence_verification.evaluate_claim`)."""

    __tablename__ = "claims"
    __table_args__ = (
        Index("ix_claims_opportunity_id", "opportunity_id"),
        Index("ix_claims_claim_type", "claim_type"),
        Index("ix_claims_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )

    claim_type: Mapped[str] = mapped_column(String(50), nullable=False)
    claim_value: Mapped[str] = mapped_column(Text, nullable=False)
    # Cleaned/canonicalized form of claim_value (e.g. an ISO date for a
    # deadline claim), when different from the raw extracted value.
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `EvidenceStatus` value -- deterministic, backend-derived. Defaults
    # to UNKNOWN: a freshly-created claim has not yet been evaluated
    # against any evidence.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=EvidenceStatus.UNKNOWN.value)

    importance: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    evidence: Mapped[list["Evidence"]] = relationship(back_populates="claim", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Claim id={self.id} type={self.claim_type!r} status={self.status!r}>"
