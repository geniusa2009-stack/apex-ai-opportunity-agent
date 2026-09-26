"""Opportunity Monitoring & Personal Impact Engine -- adds a nullable
severity classification to OpportunityChangeHistory, a nullable
monitoring-scheduling timestamp on Opportunity, and a minimal per-user
notification-preference table.

Revision ID: 0065_opportunity_monitoring
Revises: 0064_application_intelligence
Create Date: 2026-09-25

Purely additive: two nullable columns on existing tables (one indexed),
one new table. No existing column type/nullability changed, no data migrated/
rewritten. See `app/models/opportunity_change_history.py` and
`app/models/user_notification_preference.py` for the full Part C/F/O
design reasoning.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065_opportunity_monitoring"
down_revision: str | None = "0064_application_intelligence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunity_change_history",
        sa.Column("severity", sa.String(length=20), nullable=True),
    )

    op.add_column(
        "opportunities",
        sa.Column("monitoring_last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_opportunities_monitoring_last_checked_at",
        "opportunities",
        ["monitoring_last_checked_at"],
    )

    op.create_table(
        "user_notification_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("opportunity_changes", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("deadline_alerts", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("application_alerts", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("user_notification_preferences")
    op.drop_index("ix_opportunities_monitoring_last_checked_at", table_name="opportunities")
    op.drop_column("opportunities", "monitoring_last_checked_at")
    op.drop_column("opportunity_change_history", "severity")
