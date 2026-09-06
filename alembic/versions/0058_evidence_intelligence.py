"""D6 Evidence & Verification Intelligence -- `claims` / `evidence` tables.

Revision ID: 0058_evidence_intelligence
Revises: 0057_research_trajectories
Create Date: 2026-09-06

See `app/models/claim.py` and `app/models/evidence.py`'s own module
docstrings for the full Phase 1/2 investigation and design reasoning
(why this is a new, purpose-built relational chain rather than reusing
D2's opportunity-to-opportunity `entity_relationships` table).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058_evidence_intelligence"
down_revision: str | None = "0057_research_trajectories"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("claim_type", sa.String(length=50), nullable=False),
        sa.Column("claim_value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("importance", sa.String(length=20), nullable=False, server_default="normal"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_claims_opportunity_id", "claims", ["opportunity_id"])
    op.create_index("ix_claims_claim_type", "claims", ["claim_type"])
    op.create_index("ix_claims_status", "claims", ["status"])

    op.create_table(
        "evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("source_type", sa.String(length=50), nullable=True),
        sa.Column("source_authority", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supports_claim", sa.Boolean(), nullable=True),
        sa.Column("extraction_method", sa.String(length=50), nullable=False, server_default="manual"),
        sa.Column("retrieval_method", sa.String(length=50), nullable=False, server_default="automated"),
        sa.Column("evidence_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_evidence_opportunity_id", "evidence", ["opportunity_id"])
    op.create_index("ix_evidence_claim_id", "evidence", ["claim_id"])
    op.create_index("ix_evidence_source_id", "evidence", ["source_id"])
    op.create_index("ix_evidence_evidence_hash", "evidence", ["evidence_hash"])
    op.create_index("ix_evidence_retrieved_at", "evidence", ["retrieved_at"])


def downgrade() -> None:
    op.drop_index("ix_evidence_retrieved_at", table_name="evidence")
    op.drop_index("ix_evidence_evidence_hash", table_name="evidence")
    op.drop_index("ix_evidence_source_id", table_name="evidence")
    op.drop_index("ix_evidence_claim_id", table_name="evidence")
    op.drop_index("ix_evidence_opportunity_id", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_claims_status", table_name="claims")
    op.drop_index("ix_claims_claim_type", table_name="claims")
    op.drop_index("ix_claims_opportunity_id", table_name="claims")
    op.drop_table("claims")
