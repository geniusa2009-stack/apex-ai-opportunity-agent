"""Simplified Knowledge Graph -- `entity_relationships` table.

Revision ID: 0056_entity_relationships
Revises: 0055_practice_case_attempts
Create Date: 2026-09-03

See `app/models/entity_relationship.py`'s own module docstring for the
full Step 1 investigation and Step 2 design (real self-referential
Opportunity-to-Opportunity edges, real closed `relationship_type`
column, no new database technology).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056_entity_relationships"
down_revision: str | None = "0055_practice_case_attempts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "related_opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "opportunity_id", "related_opportunity_id", "relationship_type",
            name="uq_entity_relationship_edge",
        ),
    )
    op.create_index("ix_entity_relationships_opportunity_id", "entity_relationships", ["opportunity_id"])


def downgrade() -> None:
    op.drop_index("ix_entity_relationships_opportunity_id", table_name="entity_relationships")
    op.drop_table("entity_relationships")
