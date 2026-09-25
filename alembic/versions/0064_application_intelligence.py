"""Application Intelligence -- links ApplicationTracker to the catalog
Opportunity, adds an explicit submission timestamp, and adds the
application_requirement_completions table.

Revision ID: 0064_application_intelligence
Revises: 0063_opportunity_interactions
Create Date: 2026-09-25

Purely additive: two nullable columns on an existing table, one new
table, three new indexes. No existing column type/nullability changed,
no data migrated/rewritten. See `app/models/application_tracker.py` and
`app/models/application_requirement_completion.py` for the full Part
A/D/E/T design reasoning.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064_application_intelligence"
down_revision: str | None = "0063_opportunity_interactions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "application_trackers",
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "application_trackers",
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_application_trackers_opportunity_id", "application_trackers", ["opportunity_id"],
    )
    # Part T: one tracker per (user, catalog opportunity), enforced at
    # the database layer -- only when opportunity_id is actually set (a
    # freeform/off-catalog tracker has no meaningful duplicate notion).
    op.create_index(
        "uq_application_trackers_user_opportunity",
        "application_trackers", ["user_id", "opportunity_id"],
        unique=True,
        postgresql_where=sa.text("opportunity_id IS NOT NULL"),
    )

    op.create_table(
        "application_requirement_completions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tracker_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("application_trackers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement_key", sa.String(length=60), nullable=False),
        sa.Column("is_completed", sa.Boolean(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tracker_id", "requirement_key", name="uq_app_requirement_completion_tracker_key"),
    )
    op.create_index(
        "ix_app_requirement_completions_tracker_id",
        "application_requirement_completions", ["tracker_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_app_requirement_completions_tracker_id", table_name="application_requirement_completions")
    op.drop_table("application_requirement_completions")
    op.drop_index("uq_application_trackers_user_opportunity", table_name="application_trackers")
    op.drop_index("ix_application_trackers_opportunity_id", table_name="application_trackers")
    op.drop_column("application_trackers", "submitted_at")
    op.drop_column("application_trackers", "opportunity_id")
