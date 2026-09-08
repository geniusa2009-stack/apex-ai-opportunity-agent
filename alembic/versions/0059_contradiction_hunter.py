"""D7 Contradiction Hunter & Adversarial Verification -- `contradictions` table.

Revision ID: 0059_contradiction_hunter
Revises: 0058_evidence_intelligence
Create Date: 2026-09-08

See `app/models/contradiction.py` and `app/services/contradiction_hunter.py`'s
own module docstrings for the full design reasoning (why this is a new,
purpose-built record on top of D6's existing `claims`/`evidence` tables
rather than a new `status` column on `Claim`/`Evidence`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059_contradiction_hunter"
down_revision: str | None = "0058_evidence_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contradictions",
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
            "supporting_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "contradicting_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contradiction_type", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("source_authority", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("contradiction_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_contradictions_opportunity_id", "contradictions", ["opportunity_id"])
    op.create_index("ix_contradictions_claim_id", "contradictions", ["claim_id"])
    op.create_index("ix_contradictions_contradicting_evidence_id", "contradictions", ["contradicting_evidence_id"])


def downgrade() -> None:
    op.drop_index("ix_contradictions_contradicting_evidence_id", table_name="contradictions")
    op.drop_index("ix_contradictions_claim_id", table_name="contradictions")
    op.drop_index("ix_contradictions_opportunity_id", table_name="contradictions")
    op.drop_table("contradictions")
