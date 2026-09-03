"""Search Trajectory Memory -- `research_trajectories` /
`research_trajectory_steps` tables.

Revision ID: 0057_research_trajectories
Revises: 0056_entity_relationships
Create Date: 2026-09-03

See `app/models/research_trajectory.py`'s own module docstring for the
full Step 1 investigation, the disclosed `metadata`->
`trajectory_metadata`/`step_metadata` rename (SQLAlchemy reserves
`Base.metadata`), and Step 2 design reasoning.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0057_research_trajectories"
down_revision: str | None = "0056_entity_relationships"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_trajectories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("objective", sa.String(length=500), nullable=False),
        sa.Column("normalized_objective", sa.String(length=500), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
        sa.Column("total_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successful_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_steps", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opportunities_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opportunities_verified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("useful_sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("research_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("final_outcome", sa.String(length=20), nullable=True),
        sa.Column("trajectory_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_research_trajectories_normalized_objective", "research_trajectories", ["normalized_objective"]
    )
    op.create_index("ix_research_trajectories_status", "research_trajectories", ["status"])

    op.create_table(
        "research_trajectory_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "trajectory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_trajectories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(length=50), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=True),
        sa.Column("source_domain", sa.String(length=500), nullable=True),
        sa.Column("reference_id", sa.String(length=100), nullable=True),
        sa.Column(
            "parent_step_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_trajectory_steps.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="neutral"),
        sa.Column("usefulness_score", sa.Float(), nullable=True),
        sa.Column("discovered_entities_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discovered_opportunities_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("verification_impact", sa.String(length=50), nullable=True),
        sa.Column("error_reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("step_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index(
        "ix_research_trajectory_steps_trajectory_id", "research_trajectory_steps", ["trajectory_id"]
    )
    op.create_index(
        "ix_research_trajectory_steps_source_domain", "research_trajectory_steps", ["source_domain"]
    )
    op.create_index("ix_research_trajectory_steps_query", "research_trajectory_steps", ["query"])
    op.create_index(
        "ix_research_trajectory_steps_reference_id", "research_trajectory_steps", ["reference_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_research_trajectory_steps_reference_id", table_name="research_trajectory_steps")
    op.drop_index("ix_research_trajectory_steps_query", table_name="research_trajectory_steps")
    op.drop_index("ix_research_trajectory_steps_source_domain", table_name="research_trajectory_steps")
    op.drop_index("ix_research_trajectory_steps_trajectory_id", table_name="research_trajectory_steps")
    op.drop_table("research_trajectory_steps")
    op.drop_index("ix_research_trajectories_status", table_name="research_trajectories")
    op.drop_index("ix_research_trajectories_normalized_objective", table_name="research_trajectories")
    op.drop_table("research_trajectories")
