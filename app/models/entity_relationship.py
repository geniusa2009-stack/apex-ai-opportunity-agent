"""Simplified Knowledge Graph (Deliverable 2): a real Postgres table,
not a graph database.

STEP 1 INVESTIGATION (read `Opportunity`, `Source`, and every existing
opportunity-adjacent service/endpoint before writing this): no existing
table in this codebase makes "these two opportunities are related"
explicit or queryable. The relationship already implicitly exists --
two opportunities sharing `source_id` genuinely come from the same real
organization; two sharing `country` genuinely target the same real
country -- but nothing before this table let a caller ask "what else is
related to this opportunity" without re-deriving it ad hoc.

STEP 2 DESIGN: `EntityRelationship` is a real, plain, self-referential
edge table over `Opportunity` -- two real foreign keys
(`opportunity_id`/`related_opportunity_id`, both -> `opportunities.id`)
plus a real, closed `relationship_type` enum column
(`RelationshipType`, app/models/enums.py). Not a new database
technology, not a polymorphic "any entity type" edge -- every edge this
deliverable populates connects two real `Opportunity` rows, which is
exactly what Step 2's own two named examples ("opportunities sharing
the same real organization or country") already are. Edges are stored
in BOTH directions (A->B and B->A) for each real relationship, so "what
is related to opportunity X" is always a single, simple
`WHERE opportunity_id = :x` query -- no `OR related_opportunity_id = :x`
branch a future reader could forget.

SCALE BOUNDARY, DISCLOSED (see `entity_relationship_service.py`'s own
module docstring for the full reasoning): a group of opportunities that
all share one `source_id`/`country` produces edges pairwise, which is
quadratic in that group's size. A small group (a specific scraped page,
most real countries) is genuinely cheap; a very large group (e.g. every
opportunity defaulting to `country='Global'`) would not be. The
population service therefore caps how large a group it will pairwise-
connect (`_MAX_GROUP_SIZE_FOR_PAIRWISE`) and skips (with a logged,
honest reason) any group larger than that, rather than silently writing
an unbounded number of rows.
"""
import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EntityRelationship(Base):
    """One real, directed edge between two real `Opportunity` rows."""

    __tablename__ = "entity_relationships"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", "related_opportunity_id", "relationship_type",
            name="uq_entity_relationship_edge",
        ),
        Index("ix_entity_relationships_opportunity_id", "opportunity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )
    related_opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False
    )

    # Plain String, not a native Postgres enum type -- same disclosed
    # posture `Opportunity.review_status` already established (see that
    # column's own docstring): a real, closed Python-level enum
    # (`RelationshipType`) without a migration-heavy native DB enum.
    relationship_type: Mapped[str] = mapped_column(String(30), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
