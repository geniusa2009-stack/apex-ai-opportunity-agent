"""APEX User Intelligence + Proactive Guidance -- adds the new
`opportunity_interactions` append-only behavioral-event table.

Revision ID: 0063_opportunity_interactions
Revises: 0062_personalized_opportunity_profile
Create Date: 2026-09-22

Purely additive: one new table, no existing table/column touched. See
`app/models/opportunity_interaction.py`'s own docstring for the full
Part 0/D design reasoning (why this doesn't duplicate `UserBookmark`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0063_opportunity_interactions"
down_revision: str | None = "0062_personalized_opportunity_profile"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "opportunity_interactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("event_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_opportunity_interactions_user_id_created_at",
        "opportunity_interactions", ["user_id", "created_at"],
    )
    op.create_index(
        "ix_opportunity_interactions_opportunity_id_created_at",
        "opportunity_interactions", ["opportunity_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_interactions_opportunity_id_created_at", table_name="opportunity_interactions")
    op.drop_index("ix_opportunity_interactions_user_id_created_at", table_name="opportunity_interactions")
    op.drop_table("opportunity_interactions")
