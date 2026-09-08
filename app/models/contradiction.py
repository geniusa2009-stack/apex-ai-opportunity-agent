"""D7 (Contradiction Hunter & Adversarial Verification): one real,
detected disagreement between two pieces of real `Evidence` for the
same `Claim` -- `Contradiction {claim_id, supporting_evidence_id,
contradicting_evidence_id, contradiction_type, severity,
source_authority, detected_at, explanation}`.

Built strictly on top of D6's existing Claim -> Evidence -> Decision
machinery (`app/services/evidence_verification.py`) -- this table never
overrides `Claim.status` / `VerificationDecisionStatus`; it is a
durable, queryable RECORD of a contradiction D6's own deterministic
rules already reacted to. See `app/services/contradiction_hunter.py`'s
own module docstring for the real detection pipeline (a real web
search via `web_explorer_gateway`, an LLM CANDIDATE classification that
is then backend-validated against the raw search results before any
`Evidence`/`Contradiction` row is ever created).

`contradicting_evidence_id` is NOT nullable: every contradiction row
must point to one real, already-persisted `Evidence` row with
`supports_claim=False` -- "no source, no claim" (D6 constitution rule
1) applies equally here; a `Contradiction` is never created from an
LLM's opinion alone. `supporting_evidence_id` is nullable -- a claim
can be freshly contradicted with no supporting evidence recorded at
all (`EvidenceStatus.CONTRADICTED`).

DISCLOSED SCOPING: no separate `status`/resolution-workflow column
exists here. A `Contradiction` row's practical "is this still live"
answer is always the parent `Claim.status` (re-derived by
`evidence_verification.evaluate_claim` any time new evidence arrives)
-- adding a second, independently-tracked status here would create two
sources of truth for the same underlying question with no mechanism
keeping them in sync, the same data-integrity risk `Source.source_type`'s
own docstring already established this codebase avoids. `detected_at`
alone answers "when," and the linked claim/evidence rows answer
"still contradicted, right now."
"""

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import SourceAuthority


class Contradiction(Base):
    """One real, detected contradiction between two `Evidence` rows for
    the same `Claim`. Backend-derived only -- see this module's own
    docstring."""

    __tablename__ = "contradictions"
    __table_args__ = (
        Index("ix_contradictions_opportunity_id", "opportunity_id"),
        Index("ix_contradictions_claim_id", "claim_id"),
        Index("ix_contradictions_contradicting_evidence_id", "contradicting_evidence_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("claims.id", ondelete="CASCADE"), nullable=False
    )
    supporting_evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence.id", ondelete="SET NULL"), nullable=True
    )
    contradicting_evidence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("evidence.id", ondelete="CASCADE"), nullable=False
    )

    contradiction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    # Denormalized from the contradicting evidence's own `source_authority`
    # at detection time -- cheap querying/filtering without a join, same
    # value space as `Evidence.source_authority`.
    source_authority: Mapped[str] = mapped_column(String(20), nullable=False, default=SourceAuthority.UNKNOWN.value)

    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Short, backend-generated string built from real facts (claim type,
    # both authorities, both evidence excerpts) -- never an LLM-authored
    # explanation passed through verbatim (see
    # `contradiction_hunter._build_explanation`).
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    contradiction_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<Contradiction id={self.id} claim_id={self.claim_id} type={self.contradiction_type!r} severity={self.severity!r}>"
